"""task-io-driven runner for excel-agents.

Reads configs from infra/configs/, builds a TaskSource + AttemptSink, and
drives the existing excel_agent/engine.py subprocess once per task.

infra/configs/ is the *only* input surface for this runner: no template
file is loaded. For each task, the engine config is built by:

    1. Start with the full nested cfg dict (from configs.default.yaml +
       configs.yaml + optional --run-config).
    2. Select the active provider block (which is also the agent_type
       string the engine reads to find the block) and assemble the dict
       the engine expects.

This runner is the DB-driven path. The legacy local-YAML / dual-retry-
counter path lives in batch_automation_runner.py and is unchanged.

Usage (from excel-agents-master/):
    python -m infra.run                       # real run, uses configs.yaml if present
    python -m infra.run --dry-run             # print merged engine configs
    python -m infra.run --start 0 --end 1     # slice tasks
    python -m infra.run --task-id 42          # run one DB task by id
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.configs import (  # noqa: E402
    ConfigError,
    ensure_overrides_present,
    load_configs,
    resolve_agent_identity,
)
from task_io import AttemptResult, TaskSpec, build_sink, build_source  # noqa: E402

# Pulled in from the engine package so the run-folder layout the runner
# expects is always the layout the engine actually writes.
from excel_agent.core.file_organizer import get_run_folder_label  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("infra.run")

# Recognized values for `provider.kind`. Each is also the engine's
# agent_type string AND the key of the matching provider block in cfg.
_KNOWN_PROVIDERS = {"claude_excel_agent", "chatgpt_excel_agent", "tabai"}

# If a --run-config file has any of these at top level, treat it as a YAML
# task file (hand it to YamlTaskSource) instead of a project-wide overlay.
_RUN_CONFIG_TASK_KEYS = {
    "task_name",
    "upload_files",
    "files_to_upload",
    "solution_name",
    "skip",
    "task_source",
    "tasks",
}


def _sanitize_name(name: str) -> str:
    """Mirror the rename rule in excel_agent/core/file_organizer.py
    (`safe_task_name = task_name.replace("/", "-").replace(" ", "_")`).

    DO NOT add the regex strip the gui-agents-master donor uses — the
    Excel branch's downloaded-file rename is less aggressive, and any
    extra stripping here will cause find_solution_file() to silently
    miss real solution files (sink records solution_file=None).
    """
    return name.replace("/", "-").replace(" ", "_")


def _ns_to_dict(obj):
    """Recursively convert SimpleNamespace (from load_configs) to plain dicts."""
    if isinstance(obj, SimpleNamespace):
        return {k: _ns_to_dict(v) for k, v in vars(obj).items()}
    if isinstance(obj, list):
        return [_ns_to_dict(v) for v in obj]
    return obj


def build_engine_config(cfg: SimpleNamespace, spec: TaskSpec) -> dict:
    """Assemble the full engine-input dict for one task.

    All overrides (defaults + configs.yaml + --run-config) are already
    baked into `cfg` by the loader. This function only projects the
    active provider block + task fields into the shape the engine expects:
        {agent_type, prompts, prompt_version, local_files_base, file_path,
         task_name, task_id, task_source, upload_files, solution_name,
         <agent_type>: {...provider block...}}
    """
    base = _ns_to_dict(cfg)

    agent_type = cfg.provider.kind  # e.g. "claude_excel_agent"

    engine_config: dict = {
        "agent_type": agent_type,
        "prompts": list(base.get("prompts") or []),
        "prompt_version": base.get("prompt_version"),
        "task_name": spec.task_name,
        "task_id": spec.task_id,
        "upload_files": [str(p) for p in spec.upload_files],
        agent_type: copy.deepcopy(base.get(agent_type, {}) or {}),
    }

    local_files_base = base.get("local_files_base")
    if local_files_base:
        engine_config["local_files_base"] = local_files_base

    # Pass through OneDrive base path used by the engine's task-source
    # shorthand navigation. Empty list means the engine falls back to its
    # own default — keep this configurable so DB-driven runs can point at
    # whatever OneDrive parent folder the operator uses.
    file_path = base.get("file_path")
    if file_path:
        engine_config["file_path"] = file_path

    if spec.solution_name:
        engine_config["solution_name"] = spec.solution_name

    task_source = (
        spec.metadata.get("task_source") if isinstance(spec.metadata, dict) else None
    )
    if task_source:
        engine_config["task_source"] = task_source

    return engine_config


def _resolve_upload_path(raw: str, local_files_base: str | None) -> Path:
    """Mirror the engine's resolution: relative paths resolve against
    local_files_base if set, else stay relative to CWD."""
    p = Path(raw)
    if not p.is_absolute() and local_files_base:
        p = (Path(local_files_base) / p).resolve()
    return p


def preflight_check(engine_config: dict, agent_type: str) -> list[str]:
    """Collect all problems before we touch the browser. Empty list = OK."""
    errors: list[str] = []
    section = engine_config.get(agent_type, {}) or {}

    # The engine reads config[agent_type] and crashes if the block is
    # missing entirely.
    if not section:
        errors.append(
            f"provider block {agent_type!r} is empty in cfg. Set defaults "
            f"in infra/configs/configs.default.yaml under {agent_type}: ..."
        )

    # The engine validates prompts at engine.py:1110 ("Missing required
    # section: prompts"). Catch it earlier so a 50-task DB run doesn't
    # spin up a browser before failing.
    if not engine_config.get("prompts"):
        errors.append(
            "prompts is empty. Set the `prompts:` list in your --run-config "
            "or in infra/configs/configs.yaml."
        )

    # Upload files must exist on disk, resolved the same way the engine will.
    upload_files = engine_config.get("upload_files") or []
    local_files_base = engine_config.get("local_files_base")
    for raw in upload_files:
        resolved = _resolve_upload_path(str(raw), local_files_base)
        if not resolved.exists():
            if local_files_base and not Path(raw).is_absolute():
                hint = (
                    f" (resolved from local_files_base={local_files_base!r} + "
                    f"{raw!r})"
                )
            elif not Path(raw).is_absolute():
                hint = (
                    " (relative path — set local_files_base in configs.yaml or "
                    "make the path absolute)"
                )
            else:
                hint = ""
            errors.append(f"upload file not found: {resolved}{hint}")

    return errors


def find_completion_json(log_dir: Path, task_name: str, after: datetime) -> Path | None:
    if not log_dir.exists():
        return None
    matches: list[tuple[float, Path]] = []
    for p in log_dir.glob("completion_*.json"):
        if p.stat().st_mtime < after.timestamp():
            continue
        try:
            with open(p) as f:
                data = json.load(f)
        except Exception:
            continue
        for t in data.get("tasks", []):
            if t.get("task_name") == task_name:
                matches.append((p.stat().st_mtime, p))
                break
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def _write_prompts_file(
    run_dir: Path, task_name: str, engine_config: dict, started: datetime
) -> Path | None:
    """Materialize the per-task prompt payload so the sink can upload it.

    Returns None if the engine has no prompts to log — the sink treats a
    missing path as "no prompt_files to record" rather than a failure."""
    prompts = engine_config.get("prompts") or []
    if not prompts:
        return None
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_name)
    ts = started.strftime("%Y%m%d_%H%M%S")
    path = run_dir / f"prompts_{safe_name}_{ts}.json"
    payload = {
        "prompts": prompts,
        "prompt_version": engine_config.get("prompt_version"),
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def find_solution_file(
    run_dir: Path, task_name: str, solution_name: str | None, after: datetime
) -> Path | None:
    """Locate the renamed solution file the engine wrote during this run.

    The Excel engine writes downloads at:
        {run_dir}/solutions/{ts}_{base_name}{...}.xlsx
    where {base_name} contains either solution_name or task_name, both
    passed through `_sanitize_name` (above) to mirror the engine's rename
    rule. This finder applies the same sanitization to look it up.
    """
    solutions = run_dir / "solutions"
    if not solutions.exists():
        return None
    needle = _sanitize_name(solution_name or task_name).lower()
    matches: list[tuple[float, Path]] = []
    for p in solutions.glob("*.xlsx"):
        if p.stat().st_mtime < after.timestamp():
            continue
        if needle in p.name.lower():
            matches.append((p.stat().st_mtime, p))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def run_engine(engine_config: dict, engine_script: Path, timeout: int | None) -> int:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix=f"excel_agents_{engine_config.get('task_name', 'task')}_",
        delete=False,
    ) as f:
        yaml.safe_dump(engine_config, f, default_flow_style=False)
        tmp_path = Path(f.name)
    try:
        cmd = [
            sys.executable,
            str(engine_script),
            "--config",
            str(tmp_path),
            "--no-hold",
        ]
        logger.info(f"Engine: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            print(line, end="", flush=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 124
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _resolve_run_dir(agent_type: str) -> Path:
    """Mirror engine.py's run_dir construction:
        {cwd}/{YYYYMMDD}_{folder_label}/
    """
    folder_label = get_run_folder_label(agent_type)
    return Path.cwd() / f"{datetime.now().strftime('%Y%m%d')}_{folder_label}"


def _resolve_log_dir(engine_config: dict, agent_type: str) -> Path:
    """The engine writes completion JSONs to {run_dir}/json_logs/. Some
    callers configure a separate log_directory for general logs; the
    completion JSONs always live in json_logs/ under the run dir."""
    run_dir = _resolve_run_dir(agent_type)
    return run_dir / "json_logs"


# Per-provider config keys that the preflight in this file enforces. Empty
# lists are valid — Excel agents don't have hard "must-set or crash"
# fields the way the gui-agents Claude/ChatGPT web blocks do (where
# claude_web.model=null crashes the agent). Listed here for symmetry with
# the donor and to make adding required keys later a one-line change.
PROVIDER_REQUIRED_KEYS: dict[str, list[tuple[str, ...]]] = {
    "claude_excel_agent": [],
    "chatgpt_excel_agent": [],
    "tabai": [],
}


def _confirm_tasks(specs: list[TaskSpec]) -> bool:
    """Print the loaded task list and ask the user to confirm."""
    print(f"\nAbout to run {len(specs)} task(s):")
    for i, spec in enumerate(specs):
        files = ", ".join(p.name for p in spec.upload_files) or "(no files)"
        print(f"  [{i}] {spec.task_name}  —  {files}")
    try:
        answer = input("\nProceed? [y/N]: ").strip().lower()
    except EOFError:
        # Non-interactive stdin — treat as "no" unless --yes was passed.
        return False
    return answer in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="excel-agents runner (task-io driven, DB/S3 path)"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument(
        "--run-config",
        default=None,
        help=(
            "Overlay a run-specific YAML on top of configs.yaml. The file is "
            "either (a) a sparse configs.yaml-shaped overlay (source/filters/"
            "provider/prompts/…) merged as a 3rd config layer, or (b) a "
            "YAML task file (top-level task_name/upload_files/tasks), which "
            "forces source.kind='yaml' and is read by YamlTaskSource. "
            "Relative paths resolve from the repo root."
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the interactive 'proceed?' confirmation.",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        default=None,
        help=(
            "Run exactly one task (by DB id). Pins source.filters.task_ids "
            "to this value and disables skip_already_attempted so the run "
            "proceeds even if an earlier attempt exists."
        ),
    )
    args = parser.parse_args()

    run_config_path: Path | None = None
    run_config_is_task_yaml = False
    run_config_data: dict = {}
    if args.run_config is not None:
        run_config_path = Path(args.run_config)
        if not run_config_path.is_absolute():
            run_config_path = _REPO_ROOT / run_config_path
        if not run_config_path.exists():
            logger.error(f"--run-config file not found: {run_config_path}")
            return 2
        with open(run_config_path) as f:
            run_config_data = yaml.safe_load(f) or {}
        if not isinstance(run_config_data, dict):
            logger.error(
                f"--run-config must be a YAML mapping at top level: "
                f"{run_config_path}"
            )
            return 2
        run_config_is_task_yaml = bool(_RUN_CONFIG_TASK_KEYS & set(run_config_data))

    try:
        if run_config_is_task_yaml:
            # Task-shaped file: strip reserved task fields and overlay the
            # remaining keys as a project-wide layer. YamlTaskSource then
            # reads the same file for the task definition.
            overlay_data = {
                k: v
                for k, v in run_config_data.items()
                if k not in _RUN_CONFIG_TASK_KEYS
            }
            cfg = load_configs(run_config_data=overlay_data)
        else:
            cfg = load_configs(run_config_path=run_config_path)
    except ConfigError as e:
        logger.error(f"Config load failed:\n{e}")
        return 2

    if run_config_is_task_yaml:
        cfg.source.kind = "yaml"
        cfg.source.yaml_path = str(run_config_path)

    if args.task_id is not None:
        if cfg.source.kind != "postgres_s3":
            logger.error(
                f"--task-id requires source.kind=postgres_s3 (current: "
                f"{cfg.source.kind!r}). Use a run-config with "
                f"source.kind=postgres_s3 or drop --task-id."
            )
            return 2
        filters = getattr(cfg.source, "filters", None)
        if filters is None:
            filters = SimpleNamespace()
            cfg.source.filters = filters
        filters.task_ids = [args.task_id]
        filters.skip_already_attempted = False

    agent_type = cfg.provider.kind
    if agent_type not in _KNOWN_PROVIDERS:
        logger.error(
            f"provider.kind={agent_type!r} is not recognized. "
            f"Known: {sorted(_KNOWN_PROVIDERS)}."
        )
        return 2

    engine_script = _REPO_ROOT / "excel_agent" / "engine.py"
    if not engine_script.exists():
        logger.error(f"Engine not found: {engine_script}")
        return 2

    try:
        source = build_source(cfg)
        sink = build_sink(cfg)
    except ValueError as e:
        # Build failures here are user-facing: empty required fields
        # (database.url, aws.*), unknown source/sink kinds, etc.
        logger.error(f"Source/sink build failed:\n{e}")
        return 2

    identity = resolve_agent_identity(cfg)
    logger.info(
        f"agent identity: model_name={identity.model_name!r} "
        f"agent_folder={identity.agent_folder!r} "
        f"agent_model_type={identity.agent_model_type!r}"
    )

    succeeded = failed = 0
    try:
        specs = list(source.iter_tasks())
        specs = specs[args.start : args.end]
        logger.info(f"Loaded {len(specs)} task(s) from source kind={cfg.source.kind}")

        if not specs:
            logger.warning("No tasks to run.")
            return 0

        required = [".".join(p) for p in PROVIDER_REQUIRED_KEYS.get(agent_type, [])]
        if ensure_overrides_present(
            required, context=f"Preflight for provider {agent_type!r}"
        ):
            return 0

        # Build + preflight every task BEFORE the user-confirmation prompt.
        prepared: list[tuple[TaskSpec, dict]] = []
        had_errors = False
        for spec in specs:
            engine_config = build_engine_config(cfg, spec)
            errors = preflight_check(engine_config, agent_type)
            if errors:
                had_errors = True
                logger.error(
                    f"Preflight failed for task {spec.task_name!r} "
                    f"(agent_type={agent_type!r}):"
                )
                for e in errors:
                    logger.error(f"  - {e}")
            else:
                prepared.append((spec, engine_config))
        if had_errors:
            logger.error(
                "Fix infra/configs/configs.yaml or the task YAML and re-run. "
                "configs.default.yaml lists every available key."
            )
            return 2

        if not args.dry_run and not args.yes:
            if not _confirm_tasks(specs):
                logger.info("Aborted by user.")
                return 0

        for i, (spec, engine_config) in enumerate(prepared):
            idx = args.start + i
            logger.info(f"\n{'=' * 60}\nTASK {idx}: {spec.task_name}\n{'=' * 60}")

            if args.dry_run:
                logger.info("[DRY RUN] engine_config:")
                print(yaml.safe_dump(engine_config, default_flow_style=False))
                continue

            log_dir = _resolve_log_dir(engine_config, agent_type)
            run_dir = _resolve_run_dir(agent_type)
            started = datetime.now()
            prompts_file = _write_prompts_file(
                run_dir, spec.task_name, engine_config, started
            )

            rc = run_engine(engine_config, engine_script, args.timeout)
            finished = datetime.now()
            if rc == 0:
                status = "success"
                succeeded += 1
            elif rc == 124:
                status = "timeout"
                failed += 1
            else:
                status = "failed"
                failed += 1

            result = AttemptResult(
                task_id=spec.task_id,
                task_name=spec.task_name,
                agent_model_name=identity.model_name,
                prompt_version=cfg.agent.prompt_version,
                status=status,
                solution_file=find_solution_file(
                    run_dir, spec.task_name, spec.solution_name, started
                ),
                log_file=find_completion_json(log_dir, spec.task_name, started),
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
                duration_seconds=round((finished - started).total_seconds(), 2),
                prompt_files=[prompts_file] if prompts_file else [],
                extra={
                    "return_code": rc,
                    "task_metadata": dict(spec.metadata or {}),
                },
            )
            sink.publish(result)

        logger.info(f"\nDone. succeeded={succeeded} failed={failed}")
    finally:
        source.close()
        sink.close()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
