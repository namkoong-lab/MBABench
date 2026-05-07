#!/usr/bin/env bash
# Launch (or re-provision) one EC2 box for gui-agents — end-to-end.
#
# Does everything SETUP.md used to do by hand:
#   1. Launches the EC2 instance (cloud-init installs base packages + Chrome)
#   2. rsyncs the local repo → /opt/gui-agents-master on the box
#   3. pip installs requirements, drops the queue CLI + systemd unit
#   4. Synthesizes /etc/gui-agents/secrets.env from the operator's env
#      ($BIZBENCHJUDGE_KEYS_DATABASE_URL + `aws configure get` creds)
#   5. Copies the chosen configs.yaml from --config-template
#   6. Enables and starts gui-agents-worker.service
#
# Usage:
#   ./spinup.sh --alias claude-1 \
#               --config-template ./templates/claude_sonnet46.yaml
#
# Provider is read from `provider.kind` in the config template. Re-run
# against an existing alias to update code + configs on the box.
# Refuses unless the worker is strictly idle (no current task, empty queue).
#
# Optional:
#   --key-name my-key           (default: from .aws_defaults)
#   --sg-id sg-xxxxxx           (default: from .aws_defaults)
#   --instance-type t3.medium   (default)
#   --region us-east-1          (default: $AWS_REGION, else $AWS_DEFAULT_REGION, else us-east-1)
#   --ami ami-xxx               (default: latest Ubuntu 22.04 via SSM)
#   --volume-size 30            (default: 30 GiB gp3)
#
# Requirements on your laptop:
#   - AWS CLI v2 configured (aws configure, env vars, or SSO)
#   - `aws_bootstrap.sh` run once (writes .aws_defaults with key-name /
#     sg-id / region) — OR pass those flags explicitly each time.
#   - infra/configs/configs.yaml with `database.url`, `aws.access_key_id`,
#     and `aws.secret_access_key` set. spinup reads them out and synthesizes
#     /etc/gui-agents/secrets.env on the box.
#
# Precedence for each default, highest first:
#   (1) CLI flag   (2) env var (AWS_REGION)   (3) .aws_defaults   (4) hardcoded
set -euo pipefail

# Load saved defaults from aws_bootstrap.sh if present. Sets GUI_AGENTS_*.
DEFAULTS_FILE="$(cd "$(dirname "$0")" && pwd)/.aws_defaults"
# shellcheck disable=SC1090
[[ -f "$DEFAULTS_FILE" ]] && source "$DEFAULTS_FILE"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

ALIAS=""
KEY_NAME="${GUI_AGENTS_KEY_NAME:-}"
SG_ID="${GUI_AGENTS_SG_ID:-}"
INSTANCE_TYPE="t3.medium"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-${GUI_AGENTS_REGION:-us-east-1}}}"
AMI=""
VOLUME_SIZE="30"
CONFIG_TEMPLATE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alias)           ALIAS="$2"; shift 2 ;;
    --key-name)        KEY_NAME="$2"; shift 2 ;;
    --sg-id)           SG_ID="$2"; shift 2 ;;
    --instance-type)   INSTANCE_TYPE="$2"; shift 2 ;;
    --region)          REGION="$2"; shift 2 ;;
    --ami)             AMI="$2"; shift 2 ;;
    --volume-size)     VOLUME_SIZE="$2"; shift 2 ;;
    --config-template) CONFIG_TEMPLATE="$2"; shift 2 ;;
    -h|--help)         awk '/^set -euo/{exit} /^#[^!]/{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

for req in ALIAS KEY_NAME SG_ID CONFIG_TEMPLATE; do
  if [[ -z "${!req}" ]]; then
    flag="$(printf '%s' "$req" | tr '[:upper:]_' '[:lower:]-')"
    echo "missing required --$flag (see --help)" >&2
    exit 2
  fi
done
if [[ ! -r "$CONFIG_TEMPLATE" ]]; then
  echo "--config-template not readable: $CONFIG_TEMPLATE" >&2
  exit 2
fi

# Parse provider.kind out of the template. Also serves as a YAML-validity
# check: if yaml.safe_load chokes, spinup aborts here instead of on the box.
# Accepts both the raw-scalar form (`kind: chatgpt`) and the full-leaf form
# (`kind: {value: chatgpt}`) that configs.default.yaml uses.
PROVIDER="$(python3 - "$CONFIG_TEMPLATE" <<'PY' 2>/dev/null || true
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
p = (cfg.get("provider") or {}).get("kind")
if isinstance(p, dict):
    p = p.get("value")
print(p or "")
PY
)"
if [[ -z "$PROVIDER" ]]; then
  echo "could not parse provider.kind from $CONFIG_TEMPLATE" >&2
  echo "(file must be valid YAML with provider.kind set to 'claude' or 'chatgpt')" >&2
  exit 2
fi
if [[ "$PROVIDER" != "claude" && "$PROVIDER" != "chatgpt" ]]; then
  echo "provider.kind in $CONFIG_TEMPLATE must be 'claude' or 'chatgpt', got '$PROVIDER'" >&2
  exit 2
fi

# Resolve the agent identity the box will publish to state.json + DB.
# Runs the real load_configs + resolver against the template so any missing
# behavior fields (e.g. no chatgpt_web block, unknown model/agent_mode
# combo) fail here instead of after we've launched an EC2 instance.
AGENT_IDENTITY="$(PYTHONPATH="$REPO_ROOT" python3 - "$CONFIG_TEMPLATE" <<'PY'
import sys
from pathlib import Path
from infra.configs import load_configs, resolve_agent_identity
cfg = load_configs(override_path=Path(sys.argv[1]))
identity = resolve_agent_identity(cfg)
print(f"{identity.model_name}\t{identity.agent_folder}\t{identity.agent_model_type}")
PY
)"
if [[ -z "$AGENT_IDENTITY" ]]; then
  echo "failed to resolve agent identity from $CONFIG_TEMPLATE" >&2
  echo "(re-run the python block above without 2>/dev/null suppression to see the error)" >&2
  exit 2
fi
IFS=$'\t' read -r AGENT_MODEL_NAME AGENT_FOLDER AGENT_MODEL_TYPE <<< "$AGENT_IDENTITY"
echo "resolved agent identity:"
echo "  model_name:       $AGENT_MODEL_NAME   (→ task_attempts.agent_model_name)"
echo "  agent_folder:     $AGENT_FOLDER       (→ S3 prefix segment)"
echo "  agent_model_type: $AGENT_MODEL_TYPE   (→ task_attempts.agent_model_type)"

# Collect operator credentials from the laptop's infra/configs/configs.yaml
# up front. Fail fast rather than halfway through launching a box we can't
# actually configure. Same file the operator already curates for local runs;
# accepts both raw scalars and the {value: ...} leaf form.
LAPTOP_CONFIGS="$REPO_ROOT/infra/configs/configs.yaml"
if [[ ! -r "$LAPTOP_CONFIGS" ]]; then
  echo "$LAPTOP_CONFIGS not found. Create it with database.url + aws.*" >&2
  exit 2
fi
# Emit three lines: DB, AKID, SAK, STK (STK may be empty).
CREDS="$(python3 - "$LAPTOP_CONFIGS" <<'PY' 2>/dev/null || true
import sys, yaml
def get(d, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict): return ""
        cur = cur.get(p)
    if isinstance(cur, dict): cur = cur.get("value")
    return cur or ""
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(get(cfg, "database", "url"))
print(get(cfg, "aws", "access_key_id"))
print(get(cfg, "aws", "secret_access_key"))
print(get(cfg, "aws", "session_token"))
PY
)"
# `|| :` on each read: $(...) strips trailing newlines, so a trailing empty
# field (e.g. missing session_token) leaves us short a line. Tolerate EOF
# per-read and let the validators below decide what's required.
{ IFS= read -r DB_URL || :; IFS= read -r AWS_AKID || :; IFS= read -r AWS_SAK || :; IFS= read -r AWS_STK || :; } <<< "$CREDS"
if [[ -z "${DB_URL:-}" ]]; then
  echo "database.url is not set in $LAPTOP_CONFIGS" >&2
  exit 2
fi
if [[ -z "${AWS_AKID:-}" || -z "${AWS_SAK:-}" ]]; then
  echo "aws.access_key_id / aws.secret_access_key are not set in $LAPTOP_CONFIGS" >&2
  exit 2
fi

BOXES_FILE="$(cd "$(dirname "$0")" && pwd)/boxes.yaml"
SSH_KEY_PATH="$HOME/.ssh/$KEY_NAME.pem"

# Fast keypair-fingerprint check. If local pem and AWS-side keypair have
# drifted (someone deleted+recreated the keypair on AWS, or the local pem
# was replaced) every SSH/rsync op below fails as "Permission denied
# (publickey)". Surface the real cause here instead of letting rsync at
# L273 produce the cryptic version. Skipped silently if either side is
# absent — the existing flow handles those.
#
# `aws ec2 create-key-pair` and `aws ec2 import-key-pair` use different
# fingerprint formulas — SHA1 of PKCS8-DER private key vs MD5 of public-key
# DER. Compute both forms locally and accept either match, otherwise an
# imported keypair would falsely register as drift.
if [[ -f "$SSH_KEY_PATH" ]]; then
  _local_sha1="$(openssl pkcs8 -in "$SSH_KEY_PATH" -nocrypt -topk8 -outform DER 2>/dev/null \
    | openssl sha1 -c 2>/dev/null | awk -F'= ' '{print $2}' | tr -d ' ' || true)"
  _local_md5="$(openssl pkey -in "$SSH_KEY_PATH" -pubout -outform DER 2>/dev/null \
    | openssl md5 -c 2>/dev/null | awk -F'= ' '{print $2}' | tr -d ' ' || true)"
  _aws_fp="$(aws ec2 describe-key-pairs \
      --region "$REGION" --key-names "$KEY_NAME" \
      --query 'KeyPairs[0].KeyFingerprint' --output text 2>/dev/null || true)"
  if [[ -n "$_aws_fp" && "$_aws_fp" != "None" \
        && "$_aws_fp" != "$_local_sha1" \
        && "$_aws_fp" != "$_local_md5" ]]; then
    echo "local pem and AWS keypair '$KEY_NAME' have different fingerprints:" >&2
    echo "    local SHA1 (create-key-pair form): $_local_sha1   ($SSH_KEY_PATH)" >&2
    echo "    local MD5  (import-key-pair form): $_local_md5" >&2
    echo "    AWS:                                $_aws_fp   (region=$REGION)" >&2
    echo "  Every SSH op below would fail with 'Permission denied (publickey)'." >&2
    echo "  Run ./infra/dispatcher/reset_keypair.sh to recover (terminates" >&2
    echo "  any orphaned instances and regenerates the keypair)." >&2
    exit 2
  fi
fi

# ─── SSH helpers ────────────────────────────────────────────────────────────

SSH_OPTS=(
  -i "$SSH_KEY_PATH"
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=10
  -o BatchMode=yes
)

ssh_run() {
  local host="$1"; shift
  ssh "${SSH_OPTS[@]}" "ubuntu@$host" "$@"
}

scp_to() {
  local host="$1" src="$2" dst="$3"
  scp "${SSH_OPTS[@]}" "$src" "ubuntu@$host:$dst"
}

wait_for_ssh() {
  local host="$1" tries=60
  echo "Waiting for SSH on ${host}…"
  while (( tries-- > 0 )); do
    if ssh_run "$host" "true" >/dev/null 2>&1; then return 0; fi
    sleep 5
  done
  echo "SSH never came up on $host" >&2
  return 1
}

wait_for_bootstrap() {
  local host="$1" tries=60
  echo "Waiting for cloud-init to finish (/var/lib/gui-agents/.bootstrap-done)…"
  while (( tries-- > 0 )); do
    if ssh_run "$host" "test -f /var/lib/gui-agents/.bootstrap-done" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  echo "cloud-init never finished on $host" >&2
  return 1
}

# ─── secrets.env synthesis ──────────────────────────────────────────────────

SECRETS_TMP=""
cleanup_secrets() {
  [[ -n "$SECRETS_TMP" && -f "$SECRETS_TMP" ]] && rm -f "$SECRETS_TMP"
}
trap cleanup_secrets EXIT

generate_secrets_env() {
  SECRETS_TMP="$(mktemp -t gui-agents-secrets.XXXXXX)"
  chmod 600 "$SECRETS_TMP"
  {
    echo "BIZBENCHJUDGE_KEYS_DATABASE_URL=$DB_URL"
    echo "AWS_ACCESS_KEY_ID=$AWS_AKID"
    echo "AWS_SECRET_ACCESS_KEY=$AWS_SAK"
    # `x && y` would return non-zero when AWS_STK is empty, killing us under
    # `set -e` because this is the last command in the { } group.
    if [[ -n "$AWS_STK" ]]; then
      echo "AWS_SESSION_TOKEN=$AWS_STK"
    fi
  } > "$SECRETS_TMP"
}

# ─── idle check (strict) ────────────────────────────────────────────────────

# Exit 0 if worker has no current task AND an empty queue, else non-zero.
# If gui-agents-queue can't produce valid JSON (not installed, or a prior
# half-completed setup left it broken), treat the box as idle so a re-run
# can finish the bootstrap rather than getting stuck.
remote_is_idle() {
  local host="$1" out
  out="$(ssh_run "$host" "gui-agents-queue show 2>/dev/null" 2>/dev/null || true)"
  if [[ -z "$out" ]]; then
    echo "  (gui-agents-queue returned no output — treating as idle for re-setup)"
    return 0
  fi
  echo "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)  # unparseable → treat as idle (setup-broken)
sys.exit(0 if d.get("current") is None and not (d.get("queue") or []) else 1)
'
}

# ─── shared push_setup (fresh + re-run both call this) ──────────────────────

# Args: <ssh_host> <action: enable|restart>
push_setup() {
  local host="$1" action="$2"

  echo "── rsync $REPO_ROOT → ubuntu@$host:/opt/gui-agents-master ──"
  # --rsync-path="sudo rsync" lets the unprivileged 'ubuntu' user write into
  # /opt/. Relies on ubuntu having NOPASSWD sudo (default on the AWS Ubuntu
  # AMI). --no-owner --no-group cancels -a's implicit owner/group
  # preservation so files end up root:root rather than the laptop UID.
  rsync -az --delete \
    --rsync-path="sudo rsync" \
    --no-owner --no-group \
    --filter=':- .gitignore' --exclude='.git/' \
    -e "ssh ${SSH_OPTS[*]}" \
    "$REPO_ROOT/" "ubuntu@$host:/opt/gui-agents-master/"

  # Normalize perms explicitly — rsync's --chmod doesn't always update an
  # existing top-level directory, and on macOS the source can be 750 under
  # /Users, which would leave /opt/gui-agents-master untraversable for the
  # ubuntu ssh user (the gui-agents-queue wrapper does a `cd` into it).
  ssh_run "$host" "sudo chown -R root:root /opt/gui-agents-master && sudo chmod -R a+rX /opt/gui-agents-master"

  echo "── pip install requirements ──"
  ssh_run "$host" "sudo pip3 install -q -r /opt/gui-agents-master/requirements.txt"

  echo "── install queue CLI + systemd units ──"
  ssh_run "$host" "sudo install -m 0755 /opt/gui-agents-master/infra/worker/systemd/gui-agents-queue /usr/local/bin/gui-agents-queue"
  ssh_run "$host" "sudo install -m 0644 /opt/gui-agents-master/infra/worker/systemd/xvfb.service /etc/systemd/system/xvfb.service"
  ssh_run "$host" "sudo install -m 0644 /opt/gui-agents-master/infra/worker/systemd/gui-agents-chrome.service /etc/systemd/system/gui-agents-chrome.service"
  ssh_run "$host" "sudo install -m 0644 /opt/gui-agents-master/infra/worker/systemd/gui-agents-worker.service /etc/systemd/system/gui-agents-worker.service"
  ssh_run "$host" "sudo install -m 0644 /opt/gui-agents-master/infra/worker/systemd/gui-agents-auth-probe.service /etc/systemd/system/gui-agents-auth-probe.service"
  ssh_run "$host" "sudo install -m 0644 /opt/gui-agents-master/infra/worker/systemd/gui-agents-auth-probe.timer /etc/systemd/system/gui-agents-auth-probe.timer"
  ssh_run "$host" "sudo sed -i 's|^# EnvironmentFile=|EnvironmentFile=|' /etc/systemd/system/gui-agents-worker.service"
  ssh_run "$host" "sudo install -d -m 0755 /etc/gui-agents"
  ssh_run "$host" "sudo install -d -m 0777 /var/lib/gui-agents"
  # state.json was created root:root 0644 on older boxes; relax it so the
  # ubuntu SSH user can open it O_RDWR via gui-agents-queue.
  ssh_run "$host" "sudo test -e /var/lib/gui-agents/state.json && sudo chmod 666 /var/lib/gui-agents/state.json || true"

  echo "── push secrets.env ──"
  generate_secrets_env
  scp_to "$host" "$SECRETS_TMP" "/tmp/gui-agents-secrets.env"
  ssh_run "$host" "sudo install -m 0600 -o root -g root /tmp/gui-agents-secrets.env /etc/gui-agents/secrets.env && rm -f /tmp/gui-agents-secrets.env"

  echo "── push configs.yaml ──"
  scp_to "$host" "$CONFIG_TEMPLATE" "/tmp/gui-agents-configs.yaml"
  ssh_run "$host" "sudo install -m 0644 /tmp/gui-agents-configs.yaml /opt/gui-agents-master/infra/configs/configs.yaml && rm -f /tmp/gui-agents-configs.yaml"

  echo "── $action gui-agents-worker.service ──"
  ssh_run "$host" "sudo systemctl daemon-reload"
  ssh_run "$host" "sudo systemctl enable --now xvfb.service"
  # Chrome lives in its own service so cookies survive worker/task
  # cgroup teardowns. On re-runs, restart so Chrome picks up any new
  # provider/profile_dir from the freshly-pushed configs.yaml.
  if [[ "$action" == "restart" ]]; then
    ssh_run "$host" "sudo systemctl restart gui-agents-chrome.service"
    ssh_run "$host" "sudo systemctl restart gui-agents-worker.service"
  else
    ssh_run "$host" "sudo systemctl enable --now gui-agents-chrome.service"
    ssh_run "$host" "sudo systemctl enable --now gui-agents-worker.service"
  fi
  # Auth-probe timer: enable on first spinup; on re-runs, restart so any
  # interval / unit-file changes in the freshly-pushed .timer take effect
  # (daemon-reload alone doesn't reschedule an already-active timer).
  if [[ "$action" == "restart" ]]; then
    ssh_run "$host" "sudo systemctl restart gui-agents-auth-probe.timer"
  else
    ssh_run "$host" "sudo systemctl enable --now gui-agents-auth-probe.timer"
  fi
  for svc in gui-agents-chrome.service gui-agents-worker.service; do
    if ! ssh_run "$host" "sudo systemctl is-active --quiet $svc"; then
      echo "$svc is not active after $action" >&2
      ssh_run "$host" "sudo systemctl status --no-pager $svc" >&2 || true
      exit 1
    fi
  done
}

# ─── re-run path: alias already registered ──────────────────────────────────

# Parse boxes.yaml for the block with our alias. Prints
#   INSTANCE_ID=<id> SSH_HOST=<host>
# to stdout if found (eval-safe), nothing otherwise.
lookup_alias() {
  [[ -s "$BOXES_FILE" ]] || return 1
  awk -v alias="$ALIAS" '
    /^[[:space:]]*-[[:space:]]*alias:[[:space:]]*/ {
      cur=$0
      sub(/^[[:space:]]*-[[:space:]]*alias:[[:space:]]*/,"",cur)
      sub(/[[:space:]]*$/,"",cur)
      found = (cur == alias)
      iid=""; sh=""
      next
    }
    found && /^[[:space:]]*instance_id:/ {
      v=$0; sub(/^[[:space:]]*instance_id:[[:space:]]*/,"",v); sub(/[[:space:]]*$/,"",v); iid=v
    }
    found && /^[[:space:]]*ssh_host:/ {
      v=$0; sub(/^[[:space:]]*ssh_host:[[:space:]]*/,"",v); sub(/[[:space:]]*$/,"",v); sh=v
    }
    found && iid!="" && sh!="" {
      print "INSTANCE_ID="iid" SSH_HOST="sh
      exit
    }
  ' "$BOXES_FILE"
}

ALIAS_HIT="$(lookup_alias || true)"

if [[ -n "$ALIAS_HIT" ]]; then
  echo "Alias '$ALIAS' already in $BOXES_FILE — re-run path."
  eval "$ALIAS_HIT"  # sets INSTANCE_ID, SSH_HOST

  STATE="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
           --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || true)"
  if [[ "$STATE" != "running" ]]; then
    echo "instance $INSTANCE_ID for alias '$ALIAS' is not running (state=$STATE)." >&2
    echo "Remove the entry from $BOXES_FILE (or run teardown.sh) and try again." >&2
    exit 1
  fi

  echo "Checking worker idle state on ${SSH_HOST}…"
  if ! remote_is_idle "$SSH_HOST"; then
    echo "Refusing to re-run: worker is not strictly idle" >&2
    echo "(has a current task and/or non-empty queue)." >&2
    echo "Inspect with: python -m infra.dispatcher.dispatch show $ALIAS" >&2
    exit 1
  fi

  push_setup "$SSH_HOST" restart

  echo
  echo "========================================================"
  echo "Re-pushed setup to existing box."
  echo "  alias:       $ALIAS"
  echo "  instance_id: $INSTANCE_ID"
  echo "  public_dns:  $SSH_HOST"
  echo "========================================================"
  exit 0
fi

# ─── fresh path: launch a new instance ──────────────────────────────────────

# Resolve latest Ubuntu 22.04 AMI if none supplied. The SSM parameter is
# maintained by Canonical; it always points at the current stable image.
if [[ -z "$AMI" ]]; then
  AMI="$(aws ssm get-parameter \
    --region "$REGION" \
    --name /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id \
    --query Parameter.Value --output text)"
  echo "Using Ubuntu 22.04 AMI: $AMI"
fi

# cloud-init installs base dependencies so the box is usable the moment
# its status goes to 'running'. Chrome is the heavyweight; everything else
# is small. Repo install / systemd unit install happens below over SSH.
USER_DATA="$(cat <<'EOF'
#cloud-config
package_update: true
package_upgrade: false
packages:
  - python3
  - python3-pip
  - git
  - rsync
  - xvfb
  - x11vnc
  - tmux
  - wget
  - ca-certificates
  - gnupg
runcmd:
  - wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  - apt-get install -y /tmp/chrome.deb
  - rm -f /tmp/chrome.deb
  - mkdir -p /var/lib/gui-agents
  - mkdir -p /etc/gui-agents
  - touch /var/lib/gui-agents/.bootstrap-done
EOF
)"

echo "Launching instance alias=$ALIAS provider=$PROVIDER type=$INSTANCE_TYPE region=$REGION"

INSTANCE_ID="$(aws ec2 run-instances \
  --region "$REGION" \
  --image-id "$AMI" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --security-group-ids "$SG_ID" \
  --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_SIZE,VolumeType=gp3,DeleteOnTermination=true}" \
  --user-data "$USER_DATA" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Project,Value=gui-agents},{Key=alias,Value=$ALIAS},{Key=provider,Value=$PROVIDER},{Key=Name,Value=gui-agents-$ALIAS}]" \
  --query 'Instances[0].InstanceId' \
  --output text)"

echo "InstanceId: $INSTANCE_ID"
echo "Waiting for instance to reach 'running'…"
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

PUBLIC_DNS="$(aws ec2 describe-instances \
  --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicDnsName' --output text)"

# Register the box in boxes.yaml *before* push_setup runs, so that a failure
# partway through still leaves the box recoverable via a re-run.
if [[ ! -s "$BOXES_FILE" ]]; then
  echo "boxes:" > "$BOXES_FILE"
fi
cat >> "$BOXES_FILE" <<EOF
  - alias: $ALIAS
    instance_id: $INSTANCE_ID
    ssh_host: $PUBLIC_DNS
    ssh_user: ubuntu
    ssh_key: ~/.ssh/$KEY_NAME.pem
EOF

wait_for_ssh "$PUBLIC_DNS"
wait_for_bootstrap "$PUBLIC_DNS"

push_setup "$PUBLIC_DNS" enable

echo
echo "========================================================"
echo "Instance launched and worker active."
echo "  alias:       $ALIAS"
echo "  instance_id: $INSTANCE_ID"
echo "  public_dns:  $PUBLIC_DNS"
echo "  boxes.yaml:  appended ($BOXES_FILE)"
echo
echo "SSH:"
echo "    ssh -i $SSH_KEY_PATH ubuntu@$PUBLIC_DNS"
echo
echo "Verify with:"
echo "    python -m infra.dispatcher.dispatch status"
echo "========================================================"
