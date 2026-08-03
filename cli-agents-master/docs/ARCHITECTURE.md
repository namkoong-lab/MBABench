# Architecture

## System Overview

The architecture has three modular layers. The **agent core** is reusable — swap the input and output layers to adapt for different benchmarks or workflows.

```
┌─ INPUT LAYER (swappable) ──────────────────────────────────┐
│                                                            │
│  AutoBatchRunner          LocalBatchRunner                  │
│  (DB + S3 pipeline)       (local folders, no credentials)  │
│  Tasks from DB,           Tasks from local dirs,            │
│  files from S3            files copied to workspace         │
│                                                            │
└────────────────────────────┬───────────────────────────────┘
                             │
┌─ AGENT CORE (reusable) ───┴───────────────────────────────┐
│                                                            │
│  TaskExecutor ── AI reasoning loop                         │
│       │          (any OpenAI-compatible or Anthropic API)   │
│  MCPClient ──── subprocess stdio                           │
│       │                                                    │
│  MCP Server ─── 21 Excel tools (FastMCP)                   │
│       │          formula validation, circular ref detect    │
│  openpyxl + LibreOffice (read/write/recalc .xlsx)         │
│                                                            │
└────────────────────────────┬───────────────────────────────┘
                             │
┌─ OUTPUT LAYER (swappable) ─┴──────────────────────────────┐
│                                                            │
│  Auto mode:   S3 upload + DB TaskAttempt row               │
│  Local mode:  results_dir/ + attempts.jsonl                │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### Adapting for Other Benchmarks

To use this framework for a different benchmark:
1. **Input layer**: Point `workspaces[].path` to your task folders (local mode), or populate the `tasks` DB table (auto mode)
2. **Agent core**: Customize prompts in `excel_cli_agent/prompts/` — the system prompt and task template control agent behavior
3. **Output layer**: Local mode writes to `results_dir/` with `attempts.jsonl` — no infrastructure setup needed
4. **MCP tools**: Add domain-specific tools in `excel_mcp_server/tools/` (see `docs/EXTENDING.md`)

## Component Guide

Each component below corresponds to a box in the system architecture diagram. Components marked **customizable** are the primary extension points for adapting the pipeline.

### Input Layer (swappable)

The input layer determines where tasks come from and how workspaces are set up. You choose one by setting `auto_mode` or `local_mode` in your config YAML.

**AutoBatchRunner** (`auto_batch_runner.py`) — Queries the PostgreSQL `tasks` table for work, downloads starting files from S3, and after execution uploads results back to S3 and writes a `task_attempts` row. Used for production benchmarking at scale. Requires DB and S3 credentials in `.env`.

**LocalBatchRunner** (`local_batch_runner.py`) — Reads task files from local folders you specify in `workspaces[].path`. No database, no S3, no cloud credentials needed. Results are saved to `results_dir/` with an `attempts.jsonl` log. Best for development, testing new prompts, or running on a single machine.

> **To customize:** Point `workspaces` to your own task folders (local mode), or populate the `tasks` DB table with your benchmark (auto mode). If you need a completely different task source (e.g., an API, a spreadsheet), subclass `BatchRunner` in `batch_runner.py`.

### Agent Core (reusable)

The agent core is the heart of the system. It doesn't know or care where tasks come from or where results go — it just receives a workspace path and runs the AI loop.

**TaskExecutor** (`task_executor.py`, ~2400 lines) — The AI reasoning engine. Each iteration: builds context (system prompt + current Excel state + recent history), calls the LLM, parses the JSON/JSONL response into tool calls, executes them via MCP, and loops until the model signals `is_complete` or `max_iterations` is reached. Supports fresh context mode (reload `.xlsx` each iteration to prevent context bloat) and multiple providers via `base_url` auto-detection.

> **To customize:** This is the most impactful component to tune. Key levers:
> - **`max_iterations`** — How many plan-execute-observe cycles the agent gets. Higher = more thorough but slower and more expensive.
> - **`fresh_context_mode`** — When `true`, the agent re-reads the `.xlsx` file each iteration instead of accumulating tool call history. Reduces context bloat for long-running tasks.
> - **`model`** / **`base_url`** — Swap the underlying LLM. Any OpenAI-compatible API works (vLLM, SGLang, OpenRouter, OpenAI). Anthropic direct is also supported with extended thinking.
> - **`reasoning_effort`** — For models that support it. `"none"` for GPT 5.2 lets the model use its full token budget for tool calls instead of internal reasoning.

**MCPClient** (`mcp_client.py`) — Launches the MCP server as a subprocess and communicates over stdio using JSON-RPC. Handles connection lifecycle, timeouts (`asyncio.wait_for`), and subprocess management. You generally don't need to modify this.

### Prompts (customizable)

Prompts are the single highest-leverage customization point. Small changes to the system prompt or task template can dramatically change agent behavior.

**System Prompt** (`prompts/system_prompt_v{N}.txt`) — Defines the agent's role, tool usage rules, quality standards, formatting criteria, and the expected JSON response schema. Currently ~866 lines (v10). Contains the rubric criteria the agent is evaluated against.

**Task Template** (`prompts/task_template_{source}_v{N}.txt`) — Injected per-task to frame the specific work. Kept intentionally short (~56 lines) — heavier templates consistently degraded performance by encouraging one-shot mega-batches instead of iterative reasoning.

> **To customize:** Create new versioned files (never edit existing ones used in production). Register in `prompt_versions.py`. Key lessons from optimization:
> - Keep the task template under 60 lines
> - Rubric criteria work best when stated near-verbatim, not paraphrased into rules
> - Completion checklists cause mega-batching — avoid them

### MCP Server & Tools (customizable)

The MCP server provides the agent's capabilities — everything the model can actually *do* in the world.

**MCP Server** (`server.py`) — FastMCP-based server that registers all 21 tools. Runs as a subprocess, receives JSON-RPC calls from MCPClient, executes them against the workbook, and returns results.

**Tool Categories** (in `tools/`):
- **File tools** (5) — `create_file`, `list_files`, `copy_file`, `get_file_metadata`, `delete_file`. File creation has a two-layer defense: the system prompt warns it's destructive, and the server hard-blocks duplicate creation.
- **Worksheet tools** (3) — `list_worksheets`, `create_worksheet`, `delete_worksheet`. Same two-layer defense against duplicate worksheets.
- **Cell Read tools** (3) — `get_cell_range`, `get_formula`, `search_worksheet`. Non-mutating reads of cell values, formulas, and content search.
- **Cell Write tools** (2) — `edit_cells` (for labels/values), `set_cell_formula` (for formulas). Both trigger LibreOffice auto-recalculation after every write.
- **Analysis tools** (5) — `get_used_range`, `scan_worksheet_structure`, `summarize_workbook_context`, `describe_worksheet`, `validate_formula`. Help the agent understand the current state of the workbook.
- **Formatting tools** (3) — `format_cells`, `freeze_panes`, `set_column_width`. Applied in later iterations, after calculation work is done.
- **Meta tools** (2) — `report_mcp_issue` (logs problems), `validate_formula` (pre-write check).

> **To customize:** Add new tools for your domain in `excel_mcp_server/tools/`. Each tool is an async function decorated with `@mcp.tool()` that returns a JSON string. See `docs/EXTENDING.md` for the template. Common extensions: adding chart generation, pivot table creation, or domain-specific validation rules.

**Formula Validator** (`formula_validator.py`) — Seven rejection gates that every formula passes through before being written: placeholder check, constant check, string check, worksheet existence, reference validation, syntax check, and circular reference detection. Rejects invalid formulas with helpful error messages that teach the agent to self-correct.

> **To customize:** Add new validation gates for domain-specific rules (e.g., requiring certain named ranges, banning specific functions, enforcing cell reference patterns).

### Core Helpers

**Workbook I/O** (`core/workbook_io.py`) — Handles all openpyxl load/save operations with fsync to prevent data loss. Every save forces a disk flush.

**LibreOffice Bridge** (`core/libreoffice_bridge.py`) — Triggers LibreOffice recalculation after formula writes. Uses a UNO bridge subprocess (`uno_recalc_helper.py`) running on system Python (required for UNO bindings). This ensures all dependent cells update immediately, so the agent sees correct values on the next read.

### Backends

**openpyxl** — Python library for reading and writing `.xlsx` files. Handles cell values, formulas, formatting, and workbook structure. Fast but doesn't evaluate formulas.

**LibreOffice Calc** (`libreoffice_calc.py`) — Persistent LibreOffice instance that recalculates all formulas after every write. Bridges the gap between openpyxl (which stores formulas) and Excel (which evaluates them). The agent sees computed values, not just formula text.

### Output Layer (swappable)

The output layer determines where results end up after execution.

**Auto mode** — Uploads `solution.xlsx`, `transcript.md`, `openai_requests.csv`, and `task.json` to S3. Creates a `task_attempts` row in PostgreSQL with cost, timing, prompt version, and S3 URIs.

**Local mode** — Copies results to `results_dir/{task_name}/`. Appends a JSON line to `results_dir/attempts.jsonl`. No cloud infrastructure needed.

> **To customize:** For a different output destination (e.g., a different cloud provider, a local database, a webhook), modify the `_save_results()` method in the relevant batch runner.

### LLM Layer (configurable via base_url)

The LLM provider is selected by the `base_url` parameter. The system auto-detects the provider and applies provider-specific parameters:

- **Anthropic** (`"anthropic"` in URL) — Uses extended thinking mode with `thinking_budget_tokens` (separate from output tokens).
- **OpenRouter** (`"openrouter"` in URL) — Prefers OpenRouter API key. Supports `reasoning_effort` parameter.
- **OpenAI-compatible** (anything else) — Standard chat completions. Works with OpenAI, vLLM, SGLang, or any compatible endpoint.

> **To customize:** Set `base_url` in your config YAML or `.env`. Add new model configurations (pricing, token limits) in `models_config.py`.

### Configuration

**config.yaml** — All runtime parameters: mode selection, model, iterations, prompt version, task filtering, output paths. See the Config Reference table below for all parameters.

**.env** — API keys and infrastructure credentials. Loaded from the working directory. Only `OPENAI_API_KEY` (or one alternative) is required for local mode.

**prompts/v{N}.txt** — Versioned prompt files. Immutable once used in production. New versions are registered in `prompt_versions.py`.

## Package Structure

```
excel-cli-agent/
├── excel_cli_agent/              # Main package
│   ├── cli.py                    # Entry point, argument parsing, routing
│   ├── task_executor.py          # AI reasoning engine (~2400 lines)
│   ├── auto_batch_runner.py      # Automated DB→S3 pipeline
│   ├── local_batch_runner.py     # Local mode (no DB/S3, JSONL logging)
│   ├── batch_runner.py           # Base batch runner (shared by auto and local)
│   ├── mcp_client.py             # MCP subprocess management
│   ├── models_config.py          # Model pricing, defaults, slugs
│   ├── prompt_versions.py        # Shared prompt version registry
│   ├── db/                       # Database models (bundled)
│   │   ├── database.py           # SQLAlchemy engine, lazy connection
│   │   └── models.py             # Task, TaskAttempt ORM models
│   └── prompts/                  # Versioned prompt files (bundled)
│       ├── system_prompt_v{N}.txt
│       ├── task_template_fmwc_v{N}.txt
│       └── task_template_wsp_v{N}.txt
│
├── excel_mcp_server/             # MCP server package
│   ├── server.py                 # Server entry point, tool registration
│   ├── formula_validator.py      # Formula syntax/function validation
│   ├── libreoffice_calc.py       # Persistent LibreOffice engine
│   ├── uno_recalc_helper.py      # UNO bridge subprocess (system python)
│   ├── core/
│   │   ├── shared_state.py       # MCP instance, global storage path
│   │   ├── workbook_io.py        # Load/save workbooks with fsync
│   │   └── libreoffice_bridge.py # Recalc trigger after formula changes
│   ├── tools/
│   │   ├── file_tools.py         # create_file, list_files, copy_file, etc.
│   │   ├── worksheet_tools.py    # list/create/delete worksheets
│   │   ├── cell_read_tools.py    # get_cell_range, get_formula, get_used_range
│   │   ├── cell_write_tools.py   # edit_cells, set_cell_formula
│   │   ├── analysis_tools.py     # scan_structure, search, summarize, describe
│   │   ├── formatting_tools.py   # format_cells, freeze_panes, set_column_width
│   │   └── meta_tools.py         # report_mcp_issue, validate_formula
│   └── helpers/
│       ├── cell_validation.py    # Cell reference parsing
│       ├── formula_evaluation.py # Formula eval helpers
│       ├── structure_analysis.py # Worksheet structure detection
│       └── type_inference.py     # Cell type detection
│
├── pyproject.toml                # Package config, deps, entry point
├── Dockerfile                    # Containerized deployment
└── examples/
    ├── test_local.yaml           # Local mode (no DB/S3 needed)
    ├── test_quick.yaml           # Auto mode, single task, 3 iters
    └── test_mini_batch.yaml      # Auto mode, 3 tasks, 3 iters
```

## Data Flow

### Local Pipeline (No Infrastructure)

```
1. CONFIG       excel-agent --batch-config local_config.yaml
                    │
2. COPY         Copy task files to fresh workspace
                    │
3. EXECUTE      TaskExecutor runs AI reasoning loop (same as auto)
                    │
4. SAVE         Copy results to results_dir/{task_name}/
                    │
5. LOG          Append attempt to results_dir/attempts.jsonl
                    │
6. CLEANUP      Optionally delete workspace
```

No database, no S3, no cloud credentials needed. Just an API key and local folders.

### Auto Pipeline (DB + S3)

```
1. CONFIG       excel-agent --batch-config config.yaml
                    │
2. DB LOOKUP    Query `tasks` table for task list
                Filter by task_source, trial count, deprecated
                    │
3. S3 DOWNLOAD  Download starting files (PDFs, xlsx) to workspace
                s3://mbabench/BizbenchV1/...
                    │
4. EXECUTE      TaskExecutor runs AI reasoning loop:
                  a. Build system prompt + task template
                  b. Send to LLM (OpenRouter/Anthropic/OpenAI)
                  c. Parse JSON response → tool calls
                  d. Execute tools via MCP server
                  e. Repeat until complete or max_iterations
                    │
5. S3 UPLOAD    Upload results:
                  - solution.xlsx
                  - openai_requests.csv
                  - task.json
                  - transcript.md
                    │
6. DB WRITE     Insert TaskAttempt row with:
                  - timing, cost, prompt_version
                  - S3 URIs for all files
                  - pass/fail status
                    │
7. CLEANUP      Delete local workspace (configurable)
```

### TaskExecutor Loop (Step 4 Detail)

```
For each iteration (up to max_iterations):
    1. Build context:
       - System prompt (from versioned .txt file)
       - PDF contents (extracted once)
       - Excel state (fresh_context_mode: reload xlsx each iteration)
       - Recent action history (last N iterations)
    2. Call LLM API → get JSON response
    3. Parse response:
       - is_complete: true → stop
       - actions: [{tool, parameters}, ...] → execute each
    4. Execute actions via MCP client → MCP server
    5. Log iteration (CSV + optional Langfuse)
```

## Component Details

### MCPClient → MCP Server Communication

The MCP server runs as a **subprocess** launched by MCPClient:

```
MCPClient                          MCP Server
   │                                   │
   │── subprocess.Popen ──────────────>│  (python server.py <storage_path>)
   │                                   │
   │── stdin: JSON-RPC request ──────>│
   │<── stdout: JSON-RPC response ────│
   │                                   │
   │── stdin: tool call ─────────────>│  (e.g., set_cell_formula)
   │                                   │── openpyxl: write formula
   │                                   │── LibreOffice: recalculate
   │                                   │── fsync: save workbook
   │<── stdout: JSON result ──────────│
```

The server is discovered via `import excel_mcp_server` — works whether installed via pip or running from source.

### MCP Tools (21)

| Category | Tools |
|----------|-------|
| **File** | `create_file`, `list_files`, `copy_file`, `get_file_metadata`, `delete_file` |
| **Worksheet** | `list_worksheets`, `create_worksheet`, `delete_worksheet` |
| **Cell Read** | `get_cell_range`, `get_formula`, `get_used_range` |
| **Cell Write** | `edit_cells`, `set_cell_formula` |
| **Analysis** | `scan_worksheet_structure`, `search_worksheet`, `summarize_workbook_context`, `describe_worksheet` |
| **Formatting** | `format_cells`, `freeze_panes`, `set_column_width` |
| **Meta** | `validate_formula`, `report_mcp_issue` |

`set_cell_formula` and `edit_cells` trigger LibreOffice auto-recalculation after every write.

### Database Schema

```
tasks (READ-ONLY)
├── id                  PK
├── task_name           varchar(512)
├── task_starting_files JSON (S3 URIs)
├── task_solution_files JSON (S3 URIs)
├── task_source         varchar(100): 'fmwc', 'modeloff', 'wsp'
├── deprecated          bool (nullable)
└── created_at          timestamp

task_attempts (WRITE)
├── id                  PK
├── task_id             FK → tasks.id
├── agent_model_name    varchar(512): e.g., 'openpyxl_openai/gpt-5.2'
├── prompt_files        JSON (S3 URIs to prompt snapshots)
├── attempt_files       JSON (S3 URIs to results)
├── start_time          timestamp
├── end_time            timestamp
├── time_taken_min      float
├── cost                float (USD)
├── agent_failed        bool
├── agent_failed_reason text
├── prompt_version      int (system_v * 100 + template_v)
└── created_at          timestamp
```

**Critical:** The `tasks` table is strictly read-only. Code only writes to `task_attempts`.

### S3 Structure

```
s3://mbabench/BizbenchV1/
├── prompts/{model}_openpyxl/
│   ├── {timestamp}_system_prompt_v{N}.txt
│   ├── {timestamp}_task_template_fmwc_v{N}.txt
│   └── {timestamp}_task_template_wsp_v{N}.txt
└── attempts/{model}_openpyxl/
    └── task_source={src}/task_id={id}/
        ├── {timestamp}_solution.xlsx
        ├── {timestamp}_task.json
        ├── {timestamp}_openai_requests.csv
        └── {timestamp}_transcript.md
```

### Prompt Versioning

Prompts are stored as `{type}_v{N}.txt` files in `excel_cli_agent/prompts/`. Each version is registered in the `PROMPT_VERSIONS` dict in `excel_cli_agent/prompt_versions.py`.

The combined version stored in the database:
```
prompt_version = system_v * 100 + template_v
```

Example: system prompt v10 + task template v4 = version 1004.

**Rule:** Never edit a versioned file once used in production. Always create a new `_v{N+1}.txt`.

## Configuration

### Config Reference (All Parameters)

Parameters are set in YAML config files. Items marked with mode indicate which mode uses them.

| Parameter | Type | Default | Mode | Description |
|-----------|------|---------|------|-------------|
| **Mode selection** | | | | |
| `local_mode` | bool | false | local | Enable local mode (no DB/S3) |
| `auto_mode` | bool | false | auto | Enable auto mode (DB + S3 pipeline) |
| `batch_name` | string | required | both | Run identifier |
| **Model** | | | | |
| `model` | string | required | both | Model slug (e.g. `openai/gpt-4o-mini`) |
| `base_url` | string | auto-detected | both | API endpoint. Omit for default (OpenRouter if key set, else OpenAI). Set for vLLM/SGLang/Anthropic |
| `max_completion_tokens` | int | 16000 | both | Max tokens for model output |
| `reasoning_effort` | string | null | both | `none`, `low`, `medium`, `high`, `xhigh` |
| `thinking_budget_tokens` | int | null | both | For Anthropic extended thinking |
| **Task input** | | | | |
| `workspaces` | list | required | local | `[{path: "./folder/"}]` — folders with task files |
| `tasks` | list | — | auto | Explicit task names from DB |
| `task_filter` | object | — | auto | Auto-discover: `{task_source: "fmwc", missing_for_model: true}` |
| `task_type` | string | `fmwc` | local | Template selection: `fmwc` or `wsp` |
| **Execution** | | | | |
| `max_iterations` | int | 30 | both | Max agent iterations per task |
| `prompt_version` | string | `v10` | both | Prompt version (see `prompt_versions.py`) |
| `fresh_context_mode` | bool | false | both | Reload xlsx each iteration |
| `enhanced_excel_context` | bool | true | both | Grid format for Excel context |
| `api_timeout_seconds` | int | 180 | both | API call timeout |
| **Output** | | | | |
| `workspace_base_dir` | string | required | both | Where fresh workspaces are created |
| `results_dir` | string | `./results` | local | Where results + attempts.jsonl are saved |
| `cleanup_workspace` | bool | true | both | Delete workspace after completion |
| **Trial management** | | | | |
| `max_trials` | int | 7 | auto | Skip task after N attempts |
| `trials_since` | string | today | auto | Only count attempts after this date |
| `agent_folder` | string | from model | auto | S3 path + DB agent_model_name |

### Local Mode Example

```yaml
local_mode: true
batch_name: "my-run"
model: "openai/gpt-4o-mini"
workspaces:
  - path: "./my_task_files/"
workspace_base_dir: "./workspaces"
results_dir: "./results"
max_iterations: 30
prompt_version: "v10"
# base_url: "http://localhost:8000/v1"  # vLLM/SGLang
```

### Auto Mode Example

```yaml
auto_mode: true
batch_name: "my-run"
model: "openai/gpt-5.2"
workspace_base_dir: "./workspaces"
tasks: ["Task-Name"]
max_trials: 7
trials_since: "2026-02-05"
max_iterations: 30
prompt_version: "v10"
# base_url: "https://openrouter.ai/api/v1"
```

### Credentials (.env)

```bash
# API key (at least one; auto-selected based on base_url)
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...       # Optional
ANTHROPIC_API_KEY=sk-ant-...       # Optional

# API endpoint (optional; auto-detects provider)
# BASE_URL=http://localhost:8000/v1

# Required for auto mode only
DATABASE_URL=postgresql://...
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
```

The `.env` file is loaded from the working directory where `excel-agent` is run.

## Key Design Patterns

- **Two-layer defense**: System prompt educates the model + MCP server validates (formula errors, circular refs, label/formula distinction)
- **Auto-recalculation**: LibreOffice recalc triggers after every formula write via `set_cell_formula` and `edit_cells`
- **fsync saves**: All workbook saves use fsync to prevent data loss
- **Fresh context mode**: Reload solution.xlsx each iteration to avoid context bloat
- **Lazy DB connection**: Database engine is only created when `SessionLocal()` is first called — import doesn't require credentials
- **Subprocess isolation**: MCP server runs in a separate process, preventing openpyxl state leaks
- **Prompt versioning**: Immutable versioned files ensure reproducibility across benchmark runs

## Deployment Notes

- **Max 4 concurrent processes per machine** — each runs its own LibreOffice instance. More causes memory pressure and crashes.
- **Credentials**: `.env` at repo root with API keys, `DATABASE_URL`, AWS keys.
- **After code changes**: always `pip install .` to update the installed package.
- **Killing a stuck run**: `kill <PID>`. LibreOffice subprocesses may linger — clean with `pkill -f soffice`.
- **Logs**: `batch_logs/batch_<timestamp>/` (per-batch reports).
- **Disk**: workspaces cleaned by default (`cleanup_workspace: true`). Set `false` to inspect solution.xlsx before S3 upload.
