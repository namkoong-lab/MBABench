# AWS + DB-backed runtime for gui-agents-master

**Status:** Phase 0a + 0b + 0c + 1 + 2 + 3c shipped; Phase 3d code landed (pending live validation); Phase 4 next. Chrome moved into its own systemd service so cookies survive worker/task cgroup teardowns.
**Last updated:** 2026-04-23

## Goals

1. Run the existing web-GUI automation (Claude.ai / ChatGPT) on AWS, unattended.
2. Replace hand-written YAML task lists with a **pluggable task source** so the batch runner can be fed from any backend (local files, Postgres + S3, SQLite, a queue, ...).
3. Provide a **Bizbench-specific** source/sink implementation that reads `tasks` rows and writes `task_attempts` rows against the existing Neon Postgres DB + `mbabench` S3 bucket, matching the conventions already used in `cli-agents-master/excel_cli_agent/auto_batch_runner.py` and `judge/operation_scripts/get_tasks.py`.
4. Touch the GUI engine as little as possible. The engine already accepts a task-config dict with `upload_files`, `solution_name`, etc. — keep that contract; only swap what produces those dicts and what consumes the resulting solution.

Non-goals: rewriting the engine, changing the prompt templates, or moving off Chrome+CDP.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  EC2 instance (Ubuntu, Xvfb + Chrome + systemd)                  │
│                                                                  │
│   ┌─────────────────────────────────────────────────┐            │
│   │  claude_web_batch_runner.py --source <kind>     │            │
│   │                                                 │            │
│   │   ┌──────────────┐    ┌──────────────────────┐  │            │
│   │   │ TaskSource   │───▶│ claude_web_engine.py │  │            │
│   │   │ .next_task() │    │ (unchanged)          │  │            │
│   │   └──────────────┘    └──────────────────────┘  │            │
│   │          ▲                       │              │            │
│   │          │                       ▼              │            │
│   │          │              ┌──────────────┐       │            │
│   │          │              │ AttemptSink  │       │            │
│   │          │              │ .publish()   │       │            │
│   │          └──────────────└──────────────┘       │            │
│   └─────────────────────────────────────────────────┘            │
└───────────────────────┬──────────────────────────────────────────┘
                        │
              ┌─────────┴──────────┐
              ▼                    ▼
   ┌────────────────────┐   ┌────────────────────┐
   │ Neon Postgres      │   │ S3 (mbabench)     │
   │  - tasks           │   │  - BizbenchV1/...  │
   │  - task_attempts   │   │    starting_files  │
   │                    │   │    attempts        │
   └────────────────────┘   └────────────────────┘
```

Everything inside the dashed box is **provider-agnostic**. The two interfaces (`TaskSource`, `AttemptSink`) are the only seam between the GUI pipeline and whatever backend you want to plug in.

---

## Part 1 — Generic interface (ship first)

### Directory layout

```
gui-agents-master/
├── claude_web_agent/                   # existing engine, unchanged
├── data/                               # local sample inputs for runs
│   └── sample/                         #   referenced by task YAMLs via relative paths
│       ├── MO15 Round 1 - Sec 2 - Bread and Butter - Workbook.xlsx
│       └── MO15 Round 1 - Sec 2 - Bread and Butter.pdf
├── task_io/                            # provider-agnostic seam
│   ├── __init__.py
│   ├── base.py                         # TaskSource, AttemptSink, TaskSpec, AttemptResult
│   ├── registry.py                     # cfg → build_source / build_sink
│   ├── sources/
│   │   ├── yaml_source.py              # reads whatever source.yaml_path points at (typically a file under run_configs/)
│   │   └── postgres_s3.py              # Bizbench impl (see Part 2)
│   └── sinks/
│       ├── local_sink.py
│       └── postgres_s3.py              # Bizbench impl (see Part 2)
└── infra/                              # orchestration layer
    ├── plan.md                         # this file
    ├── run.py                          # task-io-driven CLI (reads ONLY infra/configs/)
    └── configs/                        # single source of truth at runtime
        ├── __init__.py
        ├── loader.py
        ├── configs.default.yaml        # every project-wide knob, every default
        ├── configs.yaml                # sparse user overrides (gitignored)
        └── run_configs/                # "what to run this time" profiles — selected via --run-config
            ├── local_run_examples/
            │   ├── sample_task.yaml        # task-shaped: handed to YamlTaskSource
            │   └── sample_task_chatgpt.yaml # task-shaped, per-run switch to ChatGPT
            └── bizbench_run_examples/
                └── sample_bizbench.yaml     # overlay-shaped: postgres_s3 + filters

# Legacy, decoupled from infra/run.py but still used by claude_web_batch_runner.py:
tasks_configs/                          # templates + example task lists — DO NOT edit for new runs
```

**What each top-level directory is for:**

- [data/](../data/) — local input files referenced by per-task YAMLs. Paths in `upload_files` resolve against CWD (or `local_files_base` if set), so keeping sample inputs here lets task YAMLs use short relative paths like `data/sample/foo.xlsx`.
- [task_io/](../task_io/) — the provider-agnostic seam. Defines `TaskSource` / `AttemptSink` protocols and ships reference implementations. The engine pipeline never imports from `infra/`; it only sees the dicts the runner hands it.
- [infra/](./) — orchestration: config loader, CLI runner, per-task merge logic. This is the only layer that knows about `cfg` or `configs/`. Replacing it wouldn't require touching `task_io/` or `claude_web_agent/`.

### Configuration

**One source of truth.** All runtime config lives under [infra/configs/](configs/). `infra/run.py` reads nothing outside that directory. The legacy [tasks_configs/](../tasks_configs/) templates and example task lists are decoupled — they're used only by the legacy `claude_web_batch_runner.py` and should not be edited for new runs.

**Three files + one folder:**

| Path | Role | Checked in? |
|---|---|---|
| `configs.default.yaml` | Every project-wide knob and its default. Canonical schema. | yes |
| `configs.yaml` | Sparse long-lived overrides (env / machine — DB url, AWS, provider project ids). | no (gitignored) |
| `run_configs/*.yaml` | "What to run this time" profiles. Selected via `--run-config PATH`. Two flavors: *task-shaped* (handed to YamlTaskSource) or *overlay-shaped* (deep-merged as a 3rd project-wide layer). | yes (samples) |

**Merge order — three layers.** Every task in a run gets the same config:

1. `configs.default.yaml` — defaults
2. `configs.yaml` — long-lived user overrides
3. `--run-config <file>` — deep-merged as a project-wide layer on top of `configs.yaml` (applies to every task in this run)

Deep-merge at each step; later wins. There is **no per-task override layer**: all 50 tasks in a batch get the same `provider`, `prompts`, `claude_web:`, etc. If you want per-task variation, run separate invocations.

**`--run-config` routing.** When the file has any of `task_name`, `upload_files`, `files_to_upload`, `solution_name`, `skip`, `task_source`, `tasks` at its top level, it is treated as a *task-shaped* YAML: the runner strips those reserved keys, overlays the remaining keys as layer 3, and forces `source.kind='yaml'` with `source.yaml_path` pointing at the file so `YamlTaskSource` reads the reserved keys as the task definition. Otherwise it is a pure *overlay-shaped* YAML and is deep-merged at layer 3 as-is.

**Task YAML schema.** Reserved keys define the task; everything else at top level is a project-wide override. When a task-shaped `--run-config` is loaded, the two sets are split — reserved keys go to `YamlTaskSource`, the rest overlay at layer 3 (run-scoped).

| Reserved (task fields) | `task_name`, `upload_files`, `files_to_upload` (alias), `solution_name`, `skip`, `task_source` |
|---|---|
| Anything else | Overlaid at layer 3 — applies to every task in this run (e.g. `prompts:`, `claude_web:`, `chatgpt_web:`) |

Example — a task-shaped run-config that runs one task on Opus with a longer timeout:

```yaml
# infra/configs/run_configs/local_run_examples/opus_long_task.yaml
task_name: "Analyze_Annual_Report"
upload_files: ["data/annual_report.pdf"]
solution_name: "AR_Analysis"

# ── below: non-reserved keys overlay at layer 3 for this run ──
prompts:
  - "Extract every financial metric into a table."
  - "Build a DCF on a new sheet using formulas only."
claude_web:
  model: "opus_4_6"
  max_sec_per_task: 10800
```

Example — an overlay-shaped run-config that pulls Bizbench tasks from Postgres:

```yaml
# infra/configs/run_configs/bizbench_run_examples/modeloff_chatgpt.yaml
source:
  kind: postgres_s3
  schema: bizbench          # picks BizbenchPostgresS3TaskSource
  filters:
    task_sources: ["modeloff"]
    skip_already_attempted: true
provider:
  kind: "chatgpt"
agent:
  model_name: "chatgpt_web"
  agent_folder: "chatgpt_web"
```

**Loader semantics** ([loader.py](configs/loader.py)) — all three layers merged here; no task-level merge exists:

1. Read `configs.default.yaml`, optional `configs.yaml`, and the `--run-config` overlay data (either read from `run_config_path` or passed pre-parsed as `run_config_data` so callers can strip reserved task keys first).
2. Shape validation — unknown keys / malformed leaves raise `ConfigError` with paths. Each source is validated independently so errors name the offending file.
3. Deep-merge `configs.yaml` overrides then the run-config overrides into a single overrides dict (later wins), then apply to the schema.
4. `required: true` leaves must be truthy after merge.
5. `free_form: true` leaves skip shape validation (used for anything intentionally schema-less).
6. Collapse `{value, required}` to plain values; return nested `SimpleNamespace`.

**Where values live:**

| Scope | Lives in | Examples |
|---|---|---|
| Project-wide, long-lived | `configs.default.yaml` + `configs.yaml` | DB url, AWS, provider project ids, scratch paths |
| Invocation-scoped | `run_configs/*.yaml` (via `--run-config`) | `source.kind` + filters, provider switches, prompts, model overrides |
| Task identity (task-shaped run-config only) | Reserved keys in the same file | `task_name`, `upload_files`, `solution_name` |
| Secrets | `configs.yaml` OR env var named by `database.url_env` | DB connection string |

**What was cut from the previous iteration:**

- `provider.template_path` — deleted; no template file is loaded.
- `template:` group (`prompts`, `prompt_version`, `local_files_base`) — promoted to top-level of `configs.default.yaml`.
- `template_overrides:` escape hatch — deleted; per-task overrides now cover the same use case without a second merge path.
- First-class flat mirrors (`claude_web.output_folder_prefix`, etc.) — replaced by the full nested `claude_web:` block carrying every field the engine reads.
- `browser.{claude,chatgpt}_cdp_port` — moved inside each provider's own `browser:` section.

**Preflight in [run.py](run.py)** runs per-task, after the task-level merge, before touching Chrome:

- Scan for leftover placeholder strings (`your-project-id-here`, etc.) — means an override didn't reach it.
- Claude contract: `claude_web.model` non-null (null crashes the agent at `.lower()`).
- ChatGPT contract: `chatgpt_web.project_id` and `project_slug` set.
- Each `upload_files` entry must exist on disk, resolved the same way the engine resolves them (relative → against `local_files_base` if set, else CWD). Error hints at whether to set `local_files_base` or use an absolute path.

Any failure → `logger.error` each issue, `return 2`.

### The data contract

```python
# task_io/base.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Iterator, Any

@dataclass
class TaskSpec:
    """What the engine needs to run one task. Source-agnostic."""
    task_id: str                      # opaque ID; source chooses the format
    task_name: str
    upload_files: list[Path]          # local paths — source is responsible
                                      # for downloading remote blobs to disk
    solution_name: str | None = None  # base name for the output file
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata["overrides"]: dict of project-wide keys from the task YAML to
    # deep-merge onto the global cfg *for this task only* (permissive).
    # metadata also carries provenance (task_source, db ids, etc.).

@dataclass
class AttemptResult:
    """What the engine produces. Sink decides how/where to persist it."""
    task_id: str
    solution_file: Path | None        # None if the run failed
    logs: dict[str, Any]              # status, timings, errors, cost, etc.
    started_at: str                   # ISO-8601
    finished_at: str
    agent_model_name: str             # "claude_web", "chatgpt_web_agent", ...
    prompt_version: int | str | None

class TaskSource(Protocol):
    def iter_tasks(self) -> Iterator[TaskSpec]: ...
    def close(self) -> None: ...

class AttemptSink(Protocol):
    def publish(self, result: AttemptResult) -> None: ...
    def close(self) -> None: ...
```

### How the runner uses it

```python
# infra/run.py
# --run-config routing: task-shaped files get their reserved keys stripped
# and the rest overlaid; overlay-shaped files are layered as-is. Either
# way, everything collapses into a single cfg at load time.
if run_config_is_task_yaml:
    overlay = {k: v for k, v in run_config_data.items()
               if k not in _RUN_CONFIG_TASK_KEYS}
    cfg = load_configs(run_config_data=overlay)
    cfg.source.kind = "yaml"
    cfg.source.yaml_path = str(run_config_path)
else:
    cfg = load_configs(run_config_path=run_config_path)  # layers 1 + 2 + 3

source = build_source(cfg)
sink   = build_sink(cfg)

for spec in source.iter_tasks():
    engine_config = build_engine_config(cfg, spec)   # pure projection
    preflight_check(engine_config, cfg.provider.kind) or die
    result = run_engine(engine_config)               # existing engine, unchanged
    sink.publish(result)
```

`build_engine_config(cfg, spec)` is a pure projection — no merging. It selects the active provider block (`claude_web` / `chatgpt_web`) and assembles `{agent_type, prompts, task_name, task_id, upload_files, solution_name, <provider>_web: {...}}` for the engine.

No template file is read. `infra/configs/` is the entire input surface.

### Directory/file layout customization

Users customize **what to pull** and **into what layout** by implementing their own `TaskSource`. The interface gives them two levers:

1. **Which files to upload** — `TaskSpec.upload_files` is a plain list of local paths. The source builds this list however it wants (full set, filtered by extension, one file per task, grouped folders, etc.).
2. **Where files land on disk** — the source downloads remote blobs to any directory layout it chooses (typically under a scratch dir). The engine only sees the final paths.

Two reference sources ship out of the box:

- `YamlTaskSource` — reads per-task YAMLs from whatever `source.yaml_path` points at (defaults to `infra/configs/task_configs`, but `--run-config` with a task-shaped file redirects this per invocation). Accepts either a directory of one-task-per-file YAMLs or a single file with a `tasks:` list.
- `PostgresS3TaskSource` — Part 2 below.

---

## Part 2 — Bizbench implementation

### Facts to build against (extracted from existing code)

| Thing | Value | Source |
|---|---|---|
| DB | Neon Postgres | `judge/project_configs.yaml` |
| DB URL env var | `BIZBENCHJUDGE_KEYS_DATABASE_URL` | `judge/operation_scripts/get_tasks.py` |
| Scratch dir env var | `BIZBENCHJUDGE_PATHS_SCRATCH_PATH` | same |
| S3 bucket | `mbabench` | `cli-agents-master/excel_cli_agent/auto_batch_runner.py` |
| S3 attempts prefix | `BizbenchV1/attempts/{agent_folder}/task_source={src}/task_id={id}` | same |
| S3 URI format | `s3://bucket/key` | `judge/operation_scripts/get_tasks.py:parse_s3_uri` |
| `tasks` columns | `id, task_name, task_starting_files, task_solution_files, task_source, deprecated, ...` | `get_tasks.py` |
| `task_attempts` columns | `id, task_id, agent_model_name, agent_model_type, attempt_files, prompt_files, start_time, end_time, time_taken_min, cost, prompt_version, agent_failed, deprecated` | `get_tasks_and_attempts.py` |
| Existing GUI model names | `claude_web`, `GPT-5.4 (Extended Pro)`, `GPT-5.4 (Agent)` | `get_tasks_and_attempts.py:DEFAULT_MODELS` |

### `PostgresS3TaskSource`

Responsibilities:

1. **Select** tasks from `tasks` with filters: `deprecated = false`, optional `task_source IN (...)`, optional explicit `id IN (...)`, optional "not yet attempted by this agent" join against `task_attempts`.
2. **Download** each row's `task_starting_files` (list of `s3://...` URIs) to `$SCRATCH/gui/task_id={id}/starting_files/{basename}` using the existing `parse_s3_uri` / `boto3` helpers.
3. **Yield** a `TaskSpec` with `upload_files` set to the downloaded paths and `metadata` carrying `task_source`, `old_id`, etc., for the sink to reference later.

Construction-time contract (both source and sink):

- **Strict AWS credentials.** `aws.access_key_id` + `aws.secret_access_key` must resolve from `configs.yaml` or from the env vars named by `aws.*_env`. The boto3 default credential chain (`~/.aws/credentials`, IAM role, etc.) is **not** consulted. Missing values → scaffolding prompt (mirrors the `database.url` pattern via `ensure_overrides_present`) and a `ValueError` swallowed cleanly by `run.py`.
- **Preflight on boto3 client build.** The source calls `sts.get_caller_identity()` and logs `account=… arn=…` so operators see which AWS identity is live before any task download. Bad/expired credentials fail here with an actionable `ValueError`, not mid-run.

Config (YAML):
```yaml
source:
  kind: postgres_s3         # backend category
  schema: bizbench          # schema wiring (required when kind=postgres_s3)
  filters:
    task_ids: [1, 2, 3]               # or omit
    task_sources: [modeloff]          # or omit
    skip_already_attempted: true
    skip_deprecated: true
# DB url, scratch dir, agent identity, and AWS creds are read from the
# shared database.* / paths.* / agent.* / aws.* blocks.
```

The `(kind, schema)` pair is dispatched in [task_io/registry.py](../task_io/registry.py): `kind` picks the backend category (`yaml`, `postgres_s3`, …) and `schema` picks the concrete subclass wiring within that backend. `schema` is required when a kind supports multiple wirings (today only `postgres_s3` → `bizbench`) and rejected when a kind has a single implementation (`yaml`, `local`). Unknown values on either axis raise `ValueError` from `_validate_kind_schema` with the set of accepted alternatives.

Trial / idempotency behavior mirrors `AutoBatchRunner._task_has_recent_attempts` in cli-agents: skip a task if there's already a non-failed, non-deprecated attempt from the same `agent_model_name` at the current `prompt_version` within a configurable window.

### `PostgresS3AttemptSink`

Responsibilities:

1. **Upload** `result.solution_file` to `s3://mbabench/BizbenchV1/attempts/{agent_folder}/task_source={src}/task_id={id}/{timestamp}_{basename}`.
2. **Upload** the per-task JSON log (same one [completion_logger.py](../claude_web_agent/completion_logger.py) already writes) alongside.
3. **Insert** a row into `task_attempts`:

   ```sql
   INSERT INTO task_attempts (
     task_id, agent_model_name, agent_model_type,
     attempt_files, prompt_files,
     start_time, end_time, time_taken_min, cost,
     prompt_version, agent_failed, agent_failed_reason, deprecated
   ) VALUES (...)
   ```

   with `attempt_files = ARRAY['s3://mbabench/.../solution.xlsx', 's3://mbabench/.../completion_log.json']`.

In addition to the strict-credentials contract and STS identity log described above for the source, the sink also calls `s3.head_bucket(aws.s3_bucket)` at construction — a single HEAD request that validates both credential validity and bucket access in one shot, before the engine spends up to 45 min on a task that can't be persisted. Missing bucket / `AccessDenied` / bad region → `ValueError` with an actionable message.

Resolved conventions (from prior open questions):

- `agent_model_type` is hardcoded to `"gui"`.
- `cost` is always `NULL` (web GUI runs are subscription-based; distinct from the CLI runner's `0.0`).
- Failed / timeout runs still insert a row with `agent_failed=true` and `agent_failed_reason` populated from `result.status` or `result.extra`.
- Per-task metadata from the source (`task_source`, `db_task_id`, …) flows to the sink via `result.extra["task_metadata"]`, threaded in `infra/run.py` when the runner constructs the `AttemptResult`.

Config lives in the shared `agent:` / `aws:` / `database:` blocks — the `sink:` YAML only needs `kind` + `schema`. Everything else is read from the existing default/overlay blocks, so a task-source and attempt-sink that both target Postgres + S3 share the same DB url, AWS credentials, bucket, and `agent.*` identity:

```yaml
sink:
  kind: postgres_s3
  schema: bizbench
# bucket / prefix / agent identity / prompt_version are all pulled from
# aws.* + agent.* in configs.default.yaml + configs.yaml.
```

### Open questions still outstanding

- **Concurrency** — if we later parallelize writes to `task_attempts` from multiple boxes, need `SELECT ... FOR UPDATE SKIP LOCKED` on the task-claiming query. Not a blocker for the pull-less dispatch model (each box only runs tasks its queue names, which the laptop arbitrates client-side), but revisit if we ever let boxes self-claim. Defer to Phase 5.
- **Credential rotation** — resolved for now with long-lived IAM user keys (see Part 3). If/when rotation is required, upgrade to instance-profile + STS pre-start hook. Tracked, not blocking.

---

## Part 3 — AWS runtime

### Recommended: single EC2 instance, first

**Why not Fargate first:** each task is 5–45 min, Claude/ChatGPT web sessions expire if re-authenticated from a fresh fingerprint, and the value of horizontal scaling is low until the queue is deep. One persistent VM with a stable login is the path of least resistance.

**Instance shape (defaults in [spinup.sh](dispatcher/spinup.sh)):**
- `t3.medium` (2 vCPU, 4 GiB) default for a single-provider box; upgrade to `t3.large`+ if you see OOM kills under Chrome load.
- Ubuntu 22.04 LTS, 30 GiB gp3 (AMI resolved via SSM parameter, so it's always the current stable image).
- Security group: SSH (22) from your IP only. No inbound for Chrome CDP — it stays localhost.
- Tags: `Project=gui-agents`, `alias=<name>`, `provider=<claude|chatgpt>`, `Name=gui-agents-<alias>`. `teardown.sh` finds boxes by these.

**Inside the VM:**
```
systemd
  └─ xvfb.service                 : Xvfb :99 -screen 0 1920x1080x24
  └─ gui-agents-chrome.service    : exec google-chrome --remote-debugging-port=<port>
                                    --user-data-dir=<profile_dir>  (DISPLAY=:99)
                                    Port + profile resolved at start from the
                                    box's configs.yaml (one provider per box).
                                    KillSignal=SIGTERM so the cookie SQLite
                                    gets flushed before SIGKILL.
  └─ gui-agents-worker.service    : pops tasks from state.json, runs each via
                                    `systemd-run --unit=gui-agents-task-<id> --wait
                                     python -m infra.run --task-id <id>`
                                    Sets GUI_AGENTS_CHROME_MANAGED=1 (propagated
                                    into each task unit), so task code only
                                    CDP-connects — it never self-launches Chrome.
                                    (see Part 4 for the queue and worker loop)
```

**Why Chrome is its own service.** Earlier prototypes let the task launch Chrome inline (still the laptop-dev path, gated behind `GUI_AGENTS_CHROME_MANAGED`). On a box that meant Chrome landed inside the task unit's cgroup — when the transient unit was collected at task end, systemd SIGKILL'd every process in the cgroup, Chrome included. Chrome under SIGKILL doesn't flush its `Cookies` SQLite or any in-flight refresh-token rotation, which showed up as spurious logouts after each task or after a `systemctl restart gui-agents-worker` (e.g. on spinup re-run). Giving Chrome its own service puts it in its own cgroup, independent of both the worker and every task unit. `KillSignal=SIGTERM` plus the default 90s timeout lets cookies reach disk on any planned restart. A single service (not one-per-provider) is enough because each box has exactly one `provider.kind`.

**First-time login flow:**
1. SSH with `-L 5901:localhost:5901` tunnel.
2. Run x11vnc (or NICE DCV) against the existing Xvfb.
3. Connect a VNC viewer, log in to claude.ai / chatgpt.com manually.
4. Cookies persist in the `--user-data-dir`.
5. Tear down VNC; the services stay running.

Repeat every few weeks when sessions expire. (Or later: script cookie injection from a secret store.)

### How the runner is driven

See **Part 4 — Multi-box dispatch** below. In short: the box runs `gui-agents-worker.service`, which pops one task at a time from a local queue file (`/var/lib/gui-agents/state.json`) and executes it by launching `python -m infra.run --task-id <id>` inside a transient `systemd-run --unit=gui-agents-task-<id>` unit. The laptop pushes tasks into that queue over SSH via `gui-agents-queue add`. The box never polls the DB for work.

### Lifecycle scripts (all under [dispatcher/](dispatcher/))

The EC2 lifecycle is driven by four bash scripts the operator runs from the laptop. None of them talk to the worker or dispatcher runtime — they only manipulate AWS and three gitignored local state files.

| Script | What it does |
|---|---|
| [aws_bootstrap.sh](dispatcher/aws_bootstrap.sh) | One-shot prerequisites. Verifies `aws sts get-caller-identity`, detects your public IP, creates the `bizbench-gui-agents` key pair (saves `~/.ssh/bizbench-gui-agents.pem`) and `bizbench-gui-agents-sg` security group, authorizes TCP 22 from your IP. Idempotent (detects existing AWS-side resources). Writes `dispatcher/.aws_defaults` so other scripts inherit the values. |
| [spinup.sh](dispatcher/spinup.sh) | Launches one tagged `t3.medium` Ubuntu 22.04 box AND installs the worker end-to-end. Cloud-init `apt install`s Python, git, rsync, Xvfb, Google Chrome, x11vnc, tmux (readiness marker: `/var/lib/gui-agents/.bootstrap-done`). Once SSH is up, the script rsyncs the local repo to `/opt/gui-agents-master` (as `sudo rsync` via `--rsync-path`), `pip install`s requirements, drops `gui-agents-queue` + `xvfb.service` + `gui-agents-chrome.service` + `gui-agents-worker.service` + the auth-probe unit/timer, synthesizes `/etc/gui-agents/secrets.env` from the laptop's `infra/configs/configs.yaml` (DB url + AWS creds), copies the `--config-template <path>` YAML into `/opt/gui-agents-master/infra/configs/configs.yaml`, and `enable --now`s both Chrome and the worker (Chrome first so the worker starts against a reachable CDP port). Provider comes from `provider.kind` in the template. **Auto-appends** the instance to `dispatcher/boxes.yaml` before the SSH phase, so a mid-run failure is recoverable via a re-run. Re-running against an existing alias re-rsyncs + restarts both Chrome and the worker (strictly idle only: no current task, empty queue); Chrome restart picks up any provider/profile changes in the freshly-pushed `configs.yaml`. Sources `.aws_defaults` for defaults; precedence is CLI flag > env var > saved defaults > hardcoded. Per-box config templates live under [dispatcher/config_templates/](dispatcher/config_templates/). |
| [teardown.sh](dispatcher/teardown.sh) | Terminates one box (`--alias X`) or all (`--all`). Finds instances by `Project=gui-agents` tag, confirms, waits for `terminated`. Security group, key pair, local `.pem`, and `boxes.yaml` are left untouched — stale entries just show as `UNREACHABLE` until the user cleans them up. |
| [_reset_dispatcher_status.sh](dispatcher/_reset_dispatcher_status.sh) | Nuclear reset for testing the setup flow end-to-end. Terminates every `Project=gui-agents` instance, deletes the key pair + SG in AWS, deletes local `.pem`, `.aws_defaults`, and `boxes.yaml`. Underscore prefix is a convention — destructive enough that it shouldn't tab-complete alongside the non-destructive scripts. |

Compatibility note: all four scripts are POSIX-flavored bash that works under macOS's default `/bin/bash` 3.2 — no `${var,,}` (lowercase expansion), no `mapfile` (bash-4+), no `readarray`.

### Config & secrets

- **Secrets file** at `/etc/gui-agents/secrets.env` (mode 0600) is the single source of truth on the box. Populated once per box during setup, loaded by the worker systemd unit via `EnvironmentFile=`:
  ```
  BIZBENCHJUDGE_KEYS_DATABASE_URL=postgres://…
  AWS_ACCESS_KEY_ID=…
  AWS_SECRET_ACCESS_KEY=…
  ```
  These env var names match what `database.url_env` and `aws.*_env` in `configs.yaml` resolve to, so the strict-credentials contract (boto3 default chain NOT consulted) is satisfied without code changes.
- **Resolved (was an open question):** credentials delivery on EC2 uses long-lived IAM user keys injected via systemd `EnvironmentFile=`. The "IAM instance profile + STS pre-start hook" alternative is deferred to Phase 5+ — it buys rotation but adds install complexity, and we aren't rotating yet.
- **IAM policy on the user** (not the box): `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` on the `mbabench` bucket; `sts:GetCallerIdentity` is always allowed.
- **No AWS keys in the repo.** `.aws_defaults` holds *names* (key name, SG id) but not secrets.

### Local state files

Three gitignored files accumulate locally over time:

| File | Writer | Purpose |
|---|---|---|
| `infra/configs/configs.yaml` | hand-edited (first time), then `dispatch config push` | Project-wide knobs: DB url, AWS creds, agent identity. The single overrides layer on top of `configs.default.yaml`. |
| `infra/dispatcher/.aws_defaults` | `aws_bootstrap.sh` | Shell-sourceable file with `GUI_AGENTS_{REGION,KEY_NAME,SG_NAME,SG_ID}`. Sourced by `spinup.sh`, `teardown.sh`, `_reset_dispatcher_status.sh` so those scripts don't re-prompt. |
| `infra/dispatcher/boxes.yaml` | `spinup.sh` (auto-append) | The box registry `dispatch.py` reads to know which boxes exist. |

All three are in [.gitignore](../.gitignore); `_reset_dispatcher_status.sh` is the one command that wipes them together.

### Observability

- systemd journal → CloudWatch Logs agent.
- Per-task JSON log is already written by `completion_logger.py`; sink uploads it to S3 next to the solution.
- CloudWatch alarm on: "no successful attempts in 2h" (detects session expiry).

---

## Part 4 — Multi-box dispatch

### Architecture

The laptop is a **transient dispatcher**: it connects over SSH to read box state and push task assignments, then disconnects. Boxes keep working autonomously on whatever is in their local queue. The **DB is not a message bus** — operational state (who is running what, who is free, what their settings are) lives on the boxes and is read only over SSH. The DB continues to hold the final `task_attempts` row that the sink writes when a task finishes.

```
     laptop                                box 1 (EC2)
  ┌───────────────┐    ssh (transient)   ┌──────────────────────────────┐
  │ dispatch.py   │────────────────────▶│ gui-agents-queue  (ssh entry) │
  │  status       │◀───── state.json ───│   ├─ reads/mutates state.json │
  │  assign       │                     │   └─ restarts worker on cfg   │
  │  config push  │                     │ gui-agents-worker.service     │
  │  logs / cancel│                     │   └─ pops queue → systemd-run │
  └───────────────┘                     │        (per-task unit)        │
                                        └──────────────────────────────┘
                                        box 2, box N … same layout
```

### Directory layout

```
gui-agents-master/infra/
├── worker/                      # runs on each EC2 box
│   ├── __init__.py
│   ├── state.py                 # state.json read/write under flock
│   ├── queue_cli.py             # `gui-agents-queue` entry point (ssh target)
│   └── worker_loop.py           # `gui-agents-worker.service` main loop
└── dispatcher/                  # runs on the laptop
    ├── __init__.py
    ├── boxes.py                 # infra/dispatcher/boxes.yaml reader
    └── dispatch.py              # the one CLI: status / assign / cancel / config / logs
```

### state.json

Path: `/var/lib/gui-agents/state.json` (box-local). Written/read only on the box, always under `fcntl.flock` on the file itself. Never touched by the laptop directly — the laptop goes through `gui-agents-queue` over SSH.

```json
{
  "worker": {
    "worker_id": "i-0abc123",
    "hostname": "box-1",
    "provider": "claude",
    "agent_model_name": "claude_web",
    "prompt_version": 7,
    "cfg_loaded_at": "2026-04-21T14:00:00Z"
  },
  "current": {
    "task_id": "142",
    "task_name": "XYZ_Analysis",
    "started_at": "2026-04-21T14:03:11Z",
    "unit": "gui-agents-task-142",
    "pid": 8421
  },
  "queue": [
    {"task_id": "143", "task_name": "ABC_Report", "assigned_at": "2026-04-21T14:00:00Z"}
  ],
  "completed": [
    {"task_id": "141", "task_name": "Prior_Task", "started_at": "...", "finished_at": "...", "status": "success", "unit": "gui-agents-task-141"}
  ]
}
```

- `worker.*` is the box's config summary. Populated by `worker_loop.py` on startup from the merged `cfg` (default + `configs.yaml`). Refreshed after a `systemctl restart gui-agents-worker.service` (e.g. after a config change).
- `current` is `null` when the box is idle. `queue` is empty when nothing is pending.
- `completed` is unbounded — kept as an on-box audit log.

### Box-side: `gui-agents-queue` CLI

Thin wrapper over `state.py`. Both the worker loop and laptop-driven SSH calls go through it, so every mutation is serialized under the same flock.

```
gui-agents-queue show                           # prints state.json (pretty)
gui-agents-queue add <task_id> [<task_name>]    # enqueue (idempotent: dup is no-op)
gui-agents-queue remove <task_id>               # drop a queued task
gui-agents-queue clear                          # drop the entire pending queue
                                                # (current task keeps running)
gui-agents-queue config show                    # emit just worker.* (fast read
                                                # for dispatch sizing)
gui-agents-queue config push                    # read configs.yaml from stdin,
                                                # write it atomically, restart
                                                # gui-agents-worker.service
```

`config push` is the only supported way to change a box's settings remotely. The dispatcher *never* edits the box's `configs.yaml` in place — it uploads the full replacement and triggers a restart.

### Box-side: `gui-agents-worker.service`

Long-running systemd unit. Pseudocode:

```python
while True:
    # 1. refresh state.worker.* if the cfg changed (file mtime + reload).
    # 2. reconcile stale current: if state.current is set but
    #    `systemctl is-active gui-agents-task-<id>` returns inactive,
    #    mark failed (reason='worker crashed'), move to completed.
    # 3. if idle and queue non-empty:
    #        pop head → set current (under flock, atomic)
    #        systemd-run --unit=gui-agents-task-<id> --wait \
    #            python -m infra.run --task-id <id>
    #        on unit exit: read exit code → map to status →
    #            move current to completed (under flock).
    # 4. sleep 5s, loop.
```

Each task runs inside its own transient unit so `systemctl stop gui-agents-task-<id>` cancels just that task without disturbing the worker loop.

### Laptop-side: `dispatch.py`

All commands fan out over SSH using the box registry (`infra/dispatcher/boxes.yaml`, gitignored). No new infra — no dispatcher daemon, no queue service, no heartbeat.

```
dispatch status [--follow]
  # ssh each box → `gui-agents-queue show` (fast path). --follow redraws every 5s.
  # prints one row per box:
  #   alias | worker_id | agent_model (pv) | login | current+elapsed | q_len | last
  #   claude-1 | i-0abc | claude_web (v7)  | user@x.com | task=142 (06:12) | +3 | 138 ok
  #   chatgpt-1| i-0def | chatgpt_web (v7) | STALE      | idle             | +0 | 139 ok
  # login column is populated by the auth-probe timer on each box; values:
  # <email> (fresh ok), ok (no email available), STALE (last probe failed),
  # old <email> (succeeded but stale >30min), ? (no probe result yet).

dispatch show <alias>
  # full state.json for one box.

dispatch assign --n 20 [--agent <model_name>] [--task-source <src>]
  # 1. ssh each box → collect (worker_id, agent_model, prompt_version, load).
  # 2. group boxes by (agent_model, prompt_version); apply --agent filter if given.
  # 3. for each group, query DB for up to N un-attempted tasks at that
  #    (agent_model, prompt_version), filtered by --task-source.
  # 4. distribute least-loaded-first; ssh box → `gui-agents-queue add <id> <name>`.

dispatch assign --tasks 42,43 [--box <alias>]
  # explicit task IDs. If --box omitted, auto-distribute across all boxes.

dispatch backlog [--agent <model_name>] [--task-source <src>]
  # ssh each box → collect (worker_id, agent_model, prompt_version, current, queue).
  # group by (agent_model, prompt_version); per cohort, runs two COUNT(*)s:
  #   - unassigned: eligible tasks excluding task_ids currently in-flight
  #   - total:      all cohort-scope tasks (drops skip_already_attempted)
  # prints: agent  pv   boxes  in_flight  unassigned  remaining  total
  #         claude v7   4      12         138         150        300
  # (remaining = in_flight + unassigned)

dispatch cancel <alias> <task_id>
  # if task is current on that box: ssh → `systemctl stop gui-agents-task-<id>`.
  # if task is queued:              ssh → `gui-agents-queue remove <id>`.

dispatch clear <alias>
  # ssh → `gui-agents-queue clear` (queue dropped; current task still finishes).

dispatch logs <alias> [--task <id>] [-f]
  # ssh → `journalctl -u gui-agents-task-<id>` (or the worker unit).

dispatch login <alias> [--local-port N] [--no-open]
  # opens a VNC tunnel to the box so the operator can log in to claude.ai /
  # chatgpt.com inside the worker's managed Chrome.
  # 1. fetches state.json; refuses if the box has a current task (login
  #    would collide with the worker's Chrome session).
  # 2. ssh → ensures gui-agents-chrome.service is up, opens the provider's
  #    login URL as a new page over CDP (so cookies land in the profile
  #    the auth-probe reads), then starts x11vnc on :99 bound to localhost.
  # 3. forwards the VNC port to the laptop with a one-shot random password
  #    (Screen Sharing refuses passwordless VNC even on localhost).
  # 4. on exit, fires gui-agents-auth-probe.service so `dispatch status`
  #    reflects the new login state immediately.

dispatch config pull <alias>
  # ssh → reads the box's configs.yaml to a local file (for editing/diff).

dispatch config push <alias> <localfile>
  # ssh → pipes localfile into `gui-agents-queue config push`.
  # Box replaces configs.yaml atomically and restarts gui-agents-worker.service.

dispatch config diff <aliasA> <aliasB>
  # pulls both; shows unified diff. Handy for detecting drift.
```

### `infra/dispatcher/boxes.yaml`

Laptop-local registry (gitignored). `spinup.sh` auto-appends on launch; `teardown.sh` doesn't prune (stale entries are harmless — they just show as UNREACHABLE).

```yaml
boxes:
  - alias: claude-1
    instance_id: i-0abc123
    ssh_host: ec2-xxx.compute.amazonaws.com
    ssh_user: ubuntu
    ssh_key: ~/.ssh/bizbench-gui-agents.pem
  - alias: chatgpt-1
    instance_id: i-0def456
    ssh_host: ec2-yyy.compute.amazonaws.com
    ssh_user: ubuntu
    ssh_key: ~/.ssh/bizbench-gui-agents.pem
```

`dispatch` commands that take `<alias>` resolve against this file. Unknown alias → hard error.

### Backlog visibility (`dispatch backlog`)

Answers *"for each cohort of reachable boxes, how much work is still eligible?"* Reuses the same cohort-grouping and eligibility filters as `dispatch assign` so the numbers align with what a subsequent `assign` would pull.

**Per-cohort output — four counts, because they answer different operator questions:**

- `in_flight` — union of `current` + queued `task_id`s across the cohort's boxes, read from the live `state.json` responses the dispatcher already fetches during status/assign. These are already assigned but not yet attempted, so they're absent from `task_attempts` and would otherwise inflate the DB-side count.
- `unassigned` — rows matching the existing filters in `_list_eligible_tasks` (`deprecated=false`; optional `task_source`; anti-join excluding non-failed, non-deprecated attempts at this `(agent_model_name, prompt_version)`), **minus** the `in_flight` set. This is what `assign --n ∞` would pull.
- `remaining = in_flight + unassigned` — full remaining work for the cohort.
- `total` — all DB rows in cohort scope (non-deprecated, matching `--task-source`), including ones already successfully attempted. Same WHERE as `unassigned` but with `skip_already_attempted=False` and no `exclude_ids`. Answers "how big is this cohort at all?" separately from "how much is left to do?".

**Implementation sketch:**

1. New helper `_count_eligible_tasks(agent_model_name, prompt_version, task_sources, exclude_ids, skip_already_attempted=True)` in `dispatch.py`, next to `_list_eligible_tasks`. Same WHERE clauses plus `AND NOT (t.id = ANY(%(exclude_ids)s))`. `SELECT COUNT(*)` — no row materialization. Called twice per cohort: once with `exclude_ids=in_flight_ids` for `unassigned`, once with `skip_already_attempted=False` and no `exclude_ids` for `total`.
2. New `cmd_backlog(args)` — `load_boxes()` → `_fetch_all_states()` → `_group_boxes_by_agent()` (all existing helpers, same reachability/cohort logic as `cmd_assign`). Per cohort, build `in_flight_ids` from state (via `_in_flight_ids_from_states` helper) and run the two counts. Print one row per cohort plus a skipped-alias line for unreachable boxes.
3. Argparse: `bl = sub.add_parser("backlog", ...)` with `--agent` and `--task-source`; dispatch arm in `main()`.
4. (Optional piggyback) After a successful `cmd_assign`, tracks each touched cohort and per-box successful adds; runs `_count_eligible_tasks` once per touched cohort (excluding pre-existing in-flight + freshly added ids) and prints `"remaining after this batch: agent=X pv=vN -> N eligible"`. Answers the "did I drain the queue?" question that operators ask right after assigning.

**Deliberate non-goals:**

- **Not a column in `dispatch status`.** Status refreshes every 5s in `--follow`; a DB round-trip per cohort per refresh is too expensive. Backlog stays a separate, explicitly-invoked command.
- **No `--list` in v1.** `COUNT(*)` covers the operator question. Listing task IDs can be added later if someone needs it.
- **No separate bucket for "only-failed-attempts" tasks.** Those are already counted eligible by the existing anti-join (`ta.agent_failed = FALSE`); surfacing them in the UI would re-explain the SQL.

**Caveats printed alongside the output:**

- Snapshot semantics — workers may finish tasks between the state fetch and the COUNT.
- Unreachable boxes contribute nothing to `in_flight`, so their queued tasks could inflate `unassigned`. The skipped-alias list is printed so operators can interpret the number.

### Concurrency, safety, failure modes

- **state.json mutations** are always under `fcntl.flock` (shared helper in `worker/state.py`). Both the worker loop and SSH-driven CLI calls go through the same code path, so the lock covers every writer.
- **Idempotent enqueue**: `gui-agents-queue add <id>` is a no-op if the task is already in `queue` or is `current`. Safe to retry after a dropped SSH.
- **Stale current**: on every worker-loop tick, cross-check `state.current.unit` against `systemctl is-active`. If the unit is inactive but `current` is set, mark the task failed (`reason='worker crashed'`) and move to `completed`. Next iteration picks up the queue normally.
- **Config-change race**: `gui-agents-queue config push` replaces `configs.yaml` and restarts the worker. If a task is currently running when the restart fires, the transient task unit keeps running (it's independent of the worker service); the worker comes back up with new cfg and resumes popping from the queue.
- **Unreachable box**: `dispatch status` prints `UNREACHABLE` for that row; other boxes are unaffected. No central state to corrupt.
- **IP-block preflight**: `status`, `show`, and `assign` call `diagnose_connectivity()` (`infra/dispatcher/diagnostics.py`) before the SSH fan-out. If the laptop's current public IP isn't the one authorized in the security group (e.g. moved networks, VPN flap), the dispatcher fails fast with an actionable message instead of hanging on 15s SSH timeouts × N boxes. Bypass for one invocation with `DISPATCH_NO_DIAGNOSE=1`.

### What stays out of the DB

- Worker health / liveness — SSH reachability is the health signal.
- Current task per box, queue contents, completed history — all on the box, read via SSH.
- Box settings — in the box's `configs.yaml`, surfaced via `gui-agents-queue config show`.

DB still stores only the final `task_attempts` row per finished task, written by the sink exactly as in Phase 2.

---

## Phased rollout

| Phase | Deliverable | Exit criteria | Status |
|---|---|---|---|
| **0a** | `task_io/` scaffold + `TaskSpec`/`AttemptResult` + `YamlTaskSource` + `LocalAttemptSink` + `infra/run.py` | Existing YAML tasks run through the new seam | ✅ shipped |
| **0b** | Config consolidation: everything under `infra/configs/`, legacy `tasks_configs/` decoupled from `infra/run.py`, `task_configs/` folder for per-task YAMLs, permissive task-level merge | Running `python -m infra.run` reads zero files outside `infra/configs/`; a task YAML's top-level keys deep-merge onto global cfg for that task | ✅ shipped (superseded by 0c) |
| **0c** | `--run-config PATH` CLI flag + `infra/configs/run_configs/` layout. Replaces `--yaml-path`. **Dropped the per-task override layer (layer 4)** — merge model collapses to 3 layers. Task-shaped run-configs split reserved keys (→ YamlTaskSource) from non-reserved keys (→ layer 3 overlay); overlay-shaped files are layer 3 as-is | `--run-config local_run_examples/sample_task.yaml` runs the local sample end-to-end; `--run-config bizbench_run_examples/sample_bizbench.yaml` drives `postgres_s3` | ✅ shipped |
| **1** | `PostgresS3TaskSource` (read-only, no DB writes) + `LocalAttemptSink` already shipped | Can run a real Bizbench task end-to-end locally, pulling from Neon+S3, writing solution to local disk | ✅ shipped |
| **2** | `PostgresS3AttemptSink`. `agent_model_type` is always `"gui"`; `cost` is always `NULL` (GUI runs are subscription-based); failed/timeout runs still insert a row with `agent_failed=true` + `agent_failed_reason`. Per-task metadata from the source flows to the sink via `result.extra["task_metadata"]`. Strict AWS-credentials contract (no boto3 default chain) + construction-time preflight: `sts.get_caller_identity()` on both source/sink + `s3.head_bucket` on sink. `build_source`/`build_sink` ValueErrors are caught cleanly in `infra/run.py`. | Solutions land in S3 and new `task_attempts` rows appear, on laptop first | ✅ shipped |
| **3a** | Worker-side: `infra/worker/` (state.py + queue_cli.py + worker_loop.py) + `--task-id` flag on `infra/run.py`. state.json is source of truth on the box; `gui-agents-queue` CLI is the only mutation path. | Manually SSH into a laptop-local "box", run `gui-agents-queue add <id>`, worker_loop pops and executes, result lands in DB | ✅ shipped (validated transitively via Phase 3c spinup) |
| **3b** | Dispatcher-side: `infra/dispatcher/` (boxes.py + dispatch.py) + `infra/dispatcher/boxes.yaml`. Commands: `status` (with `--follow` redraw), `show`, `assign`, `cancel`, `clear`, `logs`, `login` (VNC + CDP login flow), `config pull/push/diff`. Connectivity preflight (`diagnostics.py`) short-circuits SSH fan-out on IP-block. | `dispatch status` and `dispatch assign --n N` work end-to-end against at least one box | ✅ shipped (validated transitively via Phase 3c spinup) |
| **3c** | AWS lifecycle scripts: `aws_bootstrap.sh` (key + SG + `.aws_defaults`), `spinup.sh` (t3.medium + cloud-init + SSH-phase rsync/pip/unit-install/secrets-synth/`enable --now` — all the steps formerly in `SETUP.md` — plus re-run-against-existing-alias with strict idle gate), `teardown.sh` (by tag), `_reset_dispatcher_status.sh` (nuclear reset). `SETUP.md` kept as manual-fallback / diagnosis reference. `dispatcher/config_templates/` holds per-box configs.yaml overlays consumed by `--config-template`. VNC login flow for claude.ai/chatgpt.com. | One successful unattended task run on EC2 dispatched end-to-end from the laptop | ✅ end-to-end validated (chatgpt-pro-1 re-spinup: worker active, idle, reachable via `dispatch status`) |
| **3d** | `dispatch backlog` command: per-cohort COUNT of eligible tasks (`in_flight` + `unassigned` + `remaining` + `total`), using the same `_group_boxes_by_agent` + eligibility filters as `assign`. `remaining = in_flight + unassigned` (work left); `total` drops `skip_already_attempted` for full cohort size. New `_count_eligible_tasks` helper with `exclude_ids` to subtract in-flight queue items. Optional post-`assign` "remaining after this batch: N" summary per cohort. | `dispatch backlog` returns an accurate count that matches what `assign --n ∞` would pull; fleet operators can size batches without guessing | ✅ code shipped (pending live validation) |
| **4** | Second EC2 for ChatGPT; exercise multi-box distribution, per-box config divergence | Two providers running concurrently, dispatched from one `dispatch assign` call | planning |
| **5** (optional) | `dispatch` gains `boxes.yaml` auto-discovery via AWS tags. Credential-rotation upgrade (instance-profile + STS pre-start hook). Row-level locking if we ever let boxes self-claim. (Note: `--follow` status dashboard already shipped in Phase 3b.) | Zero hand-maintained box registry; rotatable creds | planning |

Each phase is shippable on its own; stop whenever the value runs out.

---

## Risks

- **Session expiry / bot detection.** Claude.ai and chatgpt.com both get cranky about automated browsers from cloud IPs. Budget time; may need residential-style egress or NAT through a known-clean IP.
- **ToS.** Automating the web UI at volume is a gray area for both providers. For large research runs, the official APIs are more defensible — worth a sanity check before investing in Phases 3+.
- **Long tasks.** 45 min max per task means any queue lease / visibility timeout must be generous, and the runner must be idempotent (a crash mid-task must not duplicate the attempt row).
- **Schema drift.** If `tasks` / `task_attempts` columns change, `PostgresS3TaskSource/Sink` break. Keep the SQL in one file and pin to the columns we read/write; loud-fail on missing columns.
