# Web Agent Automation

Automated batch execution of AI agents that work *inside the web chat UIs* of Claude.ai and ChatGPT. The system connects to a real Chrome browser via the Chrome DevTools Protocol, navigates to the chat, uploads task files, sends one or more prompts, and downloads any Excel artifacts the model produces.

> **Looking at the BizbenchV1 repo as a whole?** See [`../AGENTS.md`](../AGENTS.md) for an orientation across all agent suites in this repo.

---

## How this compares to `excel-agents-master`

The sibling repo, [`excel-agents-master/`](../excel-agents-master/), runs AI agents *inside Excel Online add-ins* via OneDrive. Same kind of benchmark output, very different runtime.

|  | This repo (`gui-agents-master`) | Sibling (`excel-agents-master`) |
|---|---|---|
| **Where the AI runs** | Web chat UI (claude.ai, chatgpt.com) | Excel Online add-in panel |
| **Required account** | Claude.ai login or ChatGPT Plus/Pro subscription | Microsoft 365 + OneDrive |
| **Browsers** | Regular Chrome | Chrome Canary + Firefox (TabAI) |
| **Cloud orchestration** | Full EC2 dispatcher in `infra/` for multi-box scaling | None — runs only on your local machine |

→ See [`../AGENTS.md`](../AGENTS.md) for the full feature matrix and the "which suite should I pick?" guide.

---

## Two ways to run

There are two runners in this repo. **External users should always use the local runner**; the EC2 dispatcher is for the internal BizbenchV1 team.

| Runner | Audience | Tasks come from | Where it executes |
|---|---|---|---|
| **`claude_web_batch_runner.py`** | **Default — everyone** | Local YAML files (`tasks_configs/examples/*.yaml`) | One Chrome browser on your laptop |
| **`infra/run.py` + `infra/dispatcher/`** | **BizbenchV1 internal team only** | Internal Postgres + S3 | Many EC2 boxes, orchestrated from a `dispatch` CLI |

If you're outside the BizbenchV1 team and want to scale across multiple boxes, the `infra/` code is in the repo for transparency, but it depends on our internal AWS account, Postgres database, and `bizbench` S3 bucket — see the [BYO infrastructure](#byo-infrastructure-external-users) note below for what you'd need to provision yourself. Not turnkey.

---

## Prerequisites

- **Python 3.10+** (3.12 recommended)
- **[uv](https://docs.astral.sh/uv/)** package manager
- **Regular Google Chrome** (Chrome Canary v148+ has a CDP compatibility issue with Playwright — stick with the stable channel)
- **Playwright Chromium browser** binaries (installed below via `playwright install chromium`)
- **Web GUI login** to your provider — this system uses your existing Claude.ai or ChatGPT browser session, **not** API keys. There's nothing to configure with `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.
- For ChatGPT runs: a paid **ChatGPT Plus or Pro subscription** is required (Pro intelligence is a paid feature).

---

## Install

```bash
git clone <repo-url>
cd gui-agents-master
uv sync
.venv/bin/python -m playwright install chromium
# On Linux only: .venv/bin/python -m playwright install-deps chromium
```

---

## Quickstart — local (default)

This is the path everyone should start with. You launch a Chrome browser, log into your provider once, and the runner sends tasks through that browser one at a time.

### 1. Launch Chrome with CDP

The automation connects to a real Chrome browser via the Chrome DevTools Protocol. Launch Chrome with remote debugging enabled, on port 9222 with a dedicated profile directory.

**macOS:**
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=~/.chrome-web-agent \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*'
```

**Linux:**
```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=~/.chrome-web-agent \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --remote-allow-origins=*
```

**Windows (PowerShell):**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\.chrome-web-agent" `
  --no-first-run --no-default-browser-check `
  --disable-background-timer-throttling `
  --disable-backgrounding-occluded-windows `
  --disable-renderer-backgrounding `
  --remote-allow-origins=*
```

The `--user-data-dir` flag creates an isolated Chrome profile. Your login session persists across runs as long as you launch Chrome with the same directory — typically a few weeks until cookies expire. Each parallel browser instance needs its own profile dir (and its own port).

### 2. Log into the provider

In the Chrome window that just opened:

- **For Claude runs**: navigate to https://claude.ai and log in.
- **For ChatGPT runs**: navigate to https://chatgpt.com and log in (Plus or Pro account).

Leave the browser open. The runner connects to it.

### 3. Configure the project ID (per provider)

Both providers identify a "project" or "workspace" you want each task to start in. Set this once per template config — the automation uses it to keep all task conversations together and (for ChatGPT) to inherit project-level settings like the default model.

**For Claude.ai:**

1. Go to https://claude.ai/projects and either pick an existing project or create a new one (any name works).
2. Open the project. The URL looks like `https://claude.ai/project/{project_id}` — copy `{project_id}`.
3. Paste it into `tasks_configs/template_claude_web.yaml` under `claude_web.project_id`. Leave `null` to use your default Claude.ai chat instead.

**For ChatGPT:**

1. Go to https://chatgpt.com and click **Projects** in the left sidebar.
2. Create a new project (e.g. `excel-tasks`).
3. Open the project. The URL looks like `https://chatgpt.com/g/g-p-{project_id}-{slug}/project` — copy both the hex `{project_id}` (after `g-p-`) and the URL `{slug}`.
4. Paste them into `tasks_configs/template_chatgpt_web.yaml` under `chatgpt_web.project_id` and `chatgpt_web.project_slug`.

**ChatGPT model/intelligence:** the runner selects `chatgpt_web.model` and `chatgpt_web.intelligence` via the composer pill before sending the first prompt. (Agent mode no longer exists in the ChatGPT UI.)

### 4. Write a task list

Copy `tasks_configs/examples/sample_tasks.yaml` and edit:

```yaml
task_source: "my_tasks"

tasks:
  - task_name: "My_Analysis"
    upload_files:
      - "tasks/My_Analysis/problem_statement.pdf"
      - "tasks/My_Analysis/data.xlsx"
    solution_name: "My_Analysis_Solution"   # optional
```

`upload_files` paths are relative to `local_files_base` from the template (or CWD if unset). `files_to_upload` is accepted as an alias.

### 5. Run

```bash
# Claude (default provider)
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml

# ChatGPT (model + intelligence set in the template)
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml \
  --provider chatgpt

# Dry-run — preview tasks without executing
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml \
  --dry-run
```

---

## Quickstart — cloud / EC2 dispatcher

> **For the BizbenchV1 internal team.** The `infra/` directory contains a dispatcher + worker stack for orchestrating Chrome on EC2 boxes against our private Postgres + S3. See [`infra/README.md`](infra/README.md) for the operator guide. **External users:** the same code can drive your own AWS / Postgres / S3 setup, but you'll need to provision them yourself — see [BYO infrastructure](#byo-infrastructure-external-users) below.

The dispatcher CLI lives at `infra/dispatcher/dispatch.py`. The most-used commands:

```bash
python -m infra.dispatcher.dispatch status                # who's doing what
python -m infra.dispatcher.dispatch assign --n 20         # pull 20 tasks from DB, distribute
python -m infra.dispatcher.dispatch logs <alias> --task 42 -f   # tail a task's journal
python -m infra.dispatcher.dispatch login <alias>         # re-login when session expires
```

Per-box bring-up (spin up an EC2 instance, install the worker, register it in `dispatcher/boxes.yaml`):

```bash
./infra/dispatcher/spinup.sh --alias chatgpt-pro-1 \
  --config-template infra/dispatcher/config_templates/chatgpt_pro.yaml
```

See [`infra/dispatcher/common_commands.md`](infra/dispatcher/common_commands.md) for the full CLI reference and [`infra/plan.md`](infra/plan.md) for the architecture and config-layering details.

### BYO infrastructure (external users)

To run the dispatcher against your own infrastructure rather than ours, you'd need: an AWS account with EC2 permissions, a Postgres database (we use Neon), and an S3 bucket. The dispatcher and worker code is reusable, but the schema for the `tasks` and `task_attempts` tables, the S3 layout (`s3://<bucket>/<task_path>` with attempts under per-agent folders), and the bootstrap scripts assume the BizbenchV1 conventions. We don't ship a schema migration for external use — the local quickstart is the supported turnkey path for outside use.

---

## Configuration reference

### Tasks YAML format

```yaml
task_source: "my_tasks"

tasks:
  - task_name: "My_Analysis"
    upload_files:                         # files_to_upload is an accepted alias
      - "tasks/My_Analysis/problem_statement.pdf"
      - "tasks/My_Analysis/data.xlsx"
    solution_name: "My_Analysis_Solution" # optional; default is {task_name}_Solution_{agent}_Model
```

### Recommended directory layout

```
my_tasks/
├── Task-Name-1/
│   ├── problem_statement.pdf
│   └── data.xlsx
├── Task-Name-2/
│   ├── problem_statement.pdf
│   └── starting_model.xlsx
```

### Template config

Templates live in `tasks_configs/`. The `prompts` list is what gets sent to the AI — replace with your own instructions:

```yaml
# tasks_configs/template_claude_web.yaml
template:
  agent_type: "claude_web"               # or "chatgpt_web"

  # Base directory for resolving relative paths in upload_files.
  # local_files_base: "project_data/"

  prompts:
    - "Analyze the attached files. Summarize the key data and questions."
    - "Build an Excel solution on a new sheet."
    - "Create a summary sheet with your conclusions. Download the workbook."

  download_artifacts: true               # Download Excel files from chat

  claude_web:
    model: fable_5                       # fable_5 | sonnet_5 | opus_4_8 | ... | null
    effort: max                          # low | medium | high | xhigh | max | null
    project_id: "your-project-id-here"

    max_sec_per_task: 7200               # 120 min total timeout

    browser:
      type: "chrome"
      headless: false
      timeout: 30000
      cdp_port: 9222                     # Match the Chrome --remote-debugging-port

    retry:
      max_agent_attempts: 3              # Retries for agent-phase failures
      max_total_attempts: 10             # Hard cap including pipeline failures
      max_sec_per_attempt: 7200
      sleep_between_retries: 5

    output:
      folder_prefix: "claudeGUI"

    logging:
      level: "INFO"
      save_to_file: true
      log_directory: "claude_web_logs"
```

### Surface mode: Chat vs Cowork / Work

Both providers now ship a mode toggle on the home/project surface — **Chat | Cowork** on Claude, **Chat | Work** on ChatGPT. The toggle's selection **persists across sessions**, so the agent asserts it at the start of every task, in both directions (a leftover selection would silently change what a run benchmarks).

- `claude_web.mode: chat | cowork` (default `chat`). Cowork adds an action-approval dropdown; `claude_web.cowork_approval: manual | auto | skip` (default `auto` — the UI default `manual` pauses on every action and stalls unattended runs). The model/effort dropdown is identical in both modes.
- `chatgpt_web.mode: chat | work` (default `chat`). Work mode replaces the intelligence picker with an **Advanced** panel: `model` (`gpt_5_6_sol`, `gpt_5_6_terra`, `gpt_5_6_luna`, `gpt_5_5`), `effort` (`light|medium|high|xhigh|max|ultra`), and `speed` (`standard|fast`, default `standard`). The chat-mode `intelligence` key must be null in work mode. Verification uses the pill text and Advanced rows — never the power slider, which decouples once an off-slider effort (Ultra) is chosen.

The 2026-07 benchmark runs use **Cowork / Work**: Claude = `mode: cowork` + `fable_5` @ `max` (identity `claude_web_cowork_fable5_max`); ChatGPT = `mode: work` + `gpt_5_6_sol` @ `ultra`/`standard` (identity `chatgpt_web_work_gpt5.6_sol_ultra`).

### Model selection

Both providers expose **two axes**: the model and a reasoning-effort level. If a value is omitted or `null`, the runner keeps whatever is currently active in your session. Selection is verified after clicking; a mismatch fails the attempt loudly rather than silently benchmarking the wrong model.

**Claude** (`claude_web.model` + `claude_web.effort`):

| `model` | Claude.ai model | | `effort` | UI label |
|---|---|---|---|---|
| `fable_5` | Fable 5 | | `low` | Low |
| `sonnet_5` | Sonnet 5 | | `medium` | Medium |
| `opus_4_8` | Opus 4.8 | | `high` | High (UI default) |
| `opus_4_7` / `opus_4_6` / `opus_3` | Opus 4.7 / 4.6 / 3 | | `xhigh` | Extra |
| `sonnet_4_6` / `haiku_4_5` | Sonnet 4.6 / Haiku 4.5 | | `max` | Max |

`enable_extended_thinking` still applies to models with a manual Thinking switch (Opus family — the switch now lives inside the Effort submenu). Models like Fable 5 have no switch; the setting is a harmless no-op there.

**ChatGPT** (`chatgpt_web.model` + `chatgpt_web.intelligence`):

| `model` | ChatGPT model | | `intelligence` | UI label |
|---|---|---|---|---|
| `gpt_5_6_sol` | GPT-5.6 Sol | | `instant` | Instant |
| `gpt_5_5` | GPT-5.5 | | `medium` | Medium |
| `gpt_5_4` | GPT-5.4 | | `high` | High |
| `gpt_5_3` | GPT-5.3 | | `xhigh` | Extra High |
| `o3` | o3 | | `pro` | Pro |

Legacy one-axis values (`model: instant|thinking|pro`) are auto-routed to the intelligence axis with a warning.

> **Agent mode is gone.** ChatGPT removed Agent mode from the UI (~mid-2026). The `agent_mode` key is still parsed for backward compatibility but ignored by the agent (with a warning); the infra track's identity resolution refuses `agent_mode: true` outright so mislabeled rows can't reach the DB.

### "Continue" auto-retry

If the model finishes responding but no Excel file appears, the engine can automatically send a "Continue" message asking the model to complete the task and provide the Excel file.

- **ChatGPT**: up to 5 continues.
- **Claude**: up to 5 continues.
- If download buttons are visible but retrieval fails, the engine does NOT send "Continue" — that's a retrieval failure, not an incomplete model.

### Provider comparison

|  | Claude | ChatGPT |
|---|---|---|
| Flag | `--provider claude` (default) | `--provider chatgpt` |
| Template setting | `agent_type: claude_web` | `agent_type: chatgpt_web` |
| Model selection | `model: fable_5` + `effort: max` | `model: gpt_5_6_sol` + `intelligence: pro` |
| How it works | Reasoning at the chosen effort builds Excel artifacts | Reasoning at the chosen intelligence builds Excel (retrieved via backend API or file cards) |
| Typical task time | 5-30 min | 15-45 min |
| Log directory | `claude_web_logs/` | `chatgpt_web_logs/` |
| Output prefix | `claudeGUI` | `chatgptGUI` |

---

## CLI options (`claude_web_batch_runner.py`)

| Flag | Default | Description |
|---|---|---|
| `--tasks FILE` | required | Path to task list YAML |
| `--template FILE` | auto | Template config (auto-selects by provider if omitted) |
| `--provider` | `claude` | `claude` or `chatgpt` |
| `--start N` | 0 | Start from task index N |
| `--end N` | all | Stop at task index N (exclusive) |
| `--stop-on-failure` | off | Abort on first failure |
| `--dry-run` | off | Preview tasks without executing |
| `--timeout` | none | Default timeout per task in seconds |

---

## Running Claude + ChatGPT in parallel

You can run both providers simultaneously using two Chrome instances on different ports.

### Launch two Chrome instances

```bash
# Browser A — port 9222 (Claude)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=~/.chrome-web-agent-claude \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*' &

# Browser B — port 9333 (ChatGPT)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9333 \
  --user-data-dir=~/.chrome-web-agent-chatgpt \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  '--remote-allow-origins=*' &
```

Log into each provider in its own browser, then run both runners in parallel:

```bash
# Claude (port 9222)
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml &

# ChatGPT (port 9333)
.venv/bin/python claude_web_batch_runner.py \
  --tasks tasks_configs/examples/sample_tasks.yaml \
  --template tasks_configs/template_chatgpt_web.yaml \
  --provider chatgpt &

wait
```

> The batch runner does **not** auto-launch Chrome on non-default ports (anything other than 9222). Start Chrome yourself on ports like 9333, 9334, etc., and set `cdp_port` to match in the template.

---

## Output structure

Each run creates a date-prefixed output folder:

```
20260320_chatgptGUI_agent/
  solutions/        # Downloaded Excel artifacts
  json_logs/        # Per-task completion JSON logs

chatgpt_web_logs/   # Detailed per-task log files
```

---

## Troubleshooting

**Browser session expired.** Re-launch Chrome with the same `--user-data-dir` and log in again. Sessions typically last weeks but can expire after long idle periods.

**Chrome won't start / "port not open".** Make sure no other Chrome instance is using the same `--user-data-dir`:
```bash
lsof -i :9222 -sTCP:LISTEN          # what's on the port
ps aux | grep remote-debugging-port  # all debugging Chrome instances
```

**`Chrome not reachable on CDP port 9222` immediately after launching Chrome.** This is almost always a setup-vs-runtime mismatch — the launch flags and the runner's expectations have drifted. Check that:
- The Chrome you launched uses `--remote-debugging-port=9222` (or whatever is in `template.<provider>.browser.cdp_port`).
- The `--user-data-dir` matches what you used for login (sessions are scoped per profile dir).
- The Chrome binary is regular Chrome, not Canary v148+ (which has a CDP incompatibility — see next entry).
- For parallel runs, the runner's `cdp_port` matches the actual port the Chrome instance is on.

**`Protocol error (Browser.setDownloadBehavior): Browser context management is not supported`.** Chrome Canary v148+ incompatibility — switch to regular Chrome.

**`chatgpt_web.agent_mode is set but Agent mode no longer exists` (ChatGPT).** Agent mode was removed from the ChatGPT UI (~mid-2026). Set `agent_mode: false` and use `chatgpt_web.model` + `chatgpt_web.intelligence` instead.

**`0 artifact preview cards found` (ChatGPT).** The model responded with text only and didn't produce an Excel file (and the backend-API download found no sandbox .xlsx refs). Usually means the prompt didn't trigger file creation. Check the conversation in the browser.

**`You don't have access to this project` (ChatGPT).** The `project_id` in the template doesn't match the ChatGPT account logged into that browser. Each ChatGPT account has its own project IDs — update the template with the correct ID from your account's project URL.

**Playwright not installed.** If you see `playwright._impl._errors.Error: Executable doesn't exist`:
```bash
.venv/bin/python -m playwright install chromium
# Linux: also .venv/bin/python -m playwright install-deps chromium
```

---

## Architecture

The system follows a composable six-layer pipeline. Green components are user-configurable; blue components are stable framework internals.

![Architecture Diagram](docs/architecture_diagram.png)

| Layer | Role | Key files |
|---|---|---|
| **Input** | Task definitions, prompt templates, agent parameters | `tasks_configs/template_*.yaml`, `tasks_configs/examples/*.yaml` |
| **Orchestration** | Batch retry logic, subprocess isolation | `claude_web_batch_runner.py` |
| **Engine** | Single-task pipeline (setup → navigate → AI → download) | `claude_web_agent/claude_web_engine.py` |
| **Navigation** | Browser navigates to claude.ai or chatgpt.com | Engine config |
| **AI Interaction** | Claude, ChatGPT, or your custom agent | `claude_web_agent/claude_web_agent.py`, `chatgpt_web_agent.py` |
| **Output** | Downloaded Excel files, validation, JSON logs | `claude_web_agent/file_validator.py`, `completion_logger.py` |

> See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full architecture guide and instructions on adding your own provider.

---

## Project structure

```
gui-agents-master/
├── claude_web_batch_runner.py        # Local batch runner (default — both providers)
├── infra/                            # EC2 dispatcher + worker (BizbenchV1 internal team)
│   ├── README.md                     # Operator guide
│   ├── run.py                        # DB-driven per-task runner
│   ├── dispatcher/                   # Laptop-side dispatch CLI
│   └── worker/                       # Box-side worker loop + systemd units
├── claude_web_agent/
│   ├── claude_web_agent.py           # Claude.ai provider
│   ├── chatgpt_web_agent.py          # ChatGPT provider
│   ├── claude_web_engine.py          # Shared per-task engine
│   ├── browser_manager.py            # Chrome CDP connection
│   ├── completion_logger.py          # Crash-safe JSON logging
│   ├── file_validator.py             # Excel file validation
│   ├── task_status.py                # Status enums
│   └── web_agent.py                  # Abstract base class
├── tasks_configs/
│   ├── template_claude_web.yaml
│   ├── template_chatgpt_web.yaml
│   └── examples/sample_tasks.yaml
├── docs/                             # Architecture diagram + ARCHITECTURE.md
├── pyproject.toml
└── requirements.txt
```
