"""task-io-driven runner for gui-agents.

Reads configs from infra/configs/, builds a TaskSource + AttemptSink, and
drives the existing claude_web_engine.py subprocess once per task.

infra/configs/ is the *only* input surface: no template file is loaded.
For each task, the engine config is built by:

    1. Start with the full nested cfg dict (from configs.default.yaml +
       configs.yaml).
    2. Deep-merge the task's non-reserved YAML keys on top (layer 3 —
       scoped to that task only).
    3. Select the active provider block and assemble the dict the engine
       expects.

Usage (from gui-agents-master/):
    python -m infra.run                       # real run, uses configs.yaml if present
    python -m infra.run --dry-run             # print merged engine configs
    python -m infra.run --start 0 --end 1     # slice tasks
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
from claude_web_agent.claude_web_engine import _sanitize_name  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("infra.run")

PROVIDER_AGENT_TYPE = {"claude": "claude_web", "chatgpt": "chatgpt_web"}

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
        {agent_type, prompts, prompt_version, local_files_base,
         task_name, task_id, task_source, upload_files, solution_name,
         <provider>_web: {...}}
    """
    base = _ns_to_dict(cfg)

    provider = cfg.provider.kind
    agent_type = PROVIDER_AGENT_TYPE.get(provider, "claude_web")
    provider_block_key = f"{provider}_web"

    engine_config: dict = {
        "agent_type": agent_type,
        "prompts": list(base.get("prompts") or []),
        "prompt_version": base.get("prompt_version"),
    }

    # prompts_file (single source of truth for the long benchmark prompts)
    # is passed through; the engine expands it into `prompts`.
    if base.get("prompts_file"):
        engine_config["prompts_file"] = base["prompts_file"]

    engine_config |= {
        "task_name": spec.task_name,
        "task_id": spec.task_id,
        "upload_files": [str(p) for p in spec.upload_files],
        provider_block_key: copy.deepcopy(base.get(provider_block_key, {}) or {}),
    }

    local_files_base = base.get("local_files_base")
    if local_files_base:
        engine_config["local_files_base"] = local_files_base

    if spec.solution_name:
        engine_config["solution_name"] = spec.solution_name

    task_source = (
        spec.metadata.get("task_source") if isinstance(spec.metadata, dict) else None
    )
    if task_source:
        engine_config["task_source"] = task_source

    # Legacy top-level fields some engine paths still look for.
    if provider == "chatgpt":
        section = engine_config[provider_block_key]
        # download_artifacts / artifact_download_dir live at the top level
        # in the legacy template. Mirror them there for the engine.
        if "download_artifacts" in section:
            engine_config["download_artifacts"] = section["download_artifacts"]
        if "artifact_download_dir" in section:
            engine_config["artifact_download_dir"] = section["artifact_download_dir"]

    return engine_config


def _resolve_upload_path(raw: str, local_files_base: str | None) -> Path:
    """Mirror the engine's resolution: relative paths resolve against
    local_files_base if set, else stay relative to CWD."""
    p = Path(raw)
    if not p.is_absolute() and local_files_base:
        p = (Path(local_files_base) / p).resolve()
    return p


def preflight_check(engine_config: dict, provider: str) -> list[str]:
    """Collect all problems before we touch the browser. Empty list = OK."""
    errors: list[str] = []
    section_key = f"{provider}_web"
    section = engine_config.get(section_key, {}) or {}

    # Provider-specific contract checks.
    _CLAUDE_MODELS = (
        "fable_5", "sonnet_5", "haiku_4_5", "opus_4_8",
        "opus_4_7", "opus_4_6", "opus_3", "sonnet_4_6",
    )
    _EFFORTS = ("low", "medium", "high", "xhigh", "max")
    _CHATGPT_MODELS = ("gpt_5_6_sol", "gpt_5_5", "gpt_5_4", "gpt_5_3", "o3")
    _CHATGPT_WORK_MODELS = (
        "gpt_5_6_sol", "gpt_5_6_terra", "gpt_5_6_luna", "gpt_5_5",
    )
    _INTELLIGENCE = ("instant", "medium", "high", "xhigh", "pro")
    _WORK_EFFORTS = ("light", "medium", "high", "xhigh", "max", "ultra")
    _WORK_SPEEDS = ("standard", "fast")
    if provider == "claude":
        mode = (section.get("mode") or "chat").lower()
        if mode not in ("chat", "cowork"):
            errors.append(
                f"claude_web.mode={mode!r} is not 'chat' or 'cowork'."
            )
        approval = section.get("cowork_approval")
        if approval is not None and approval not in ("manual", "auto", "skip"):
            errors.append(
                f"claude_web.cowork_approval={approval!r} is not one of "
                "manual, auto, skip (or null = auto)."
            )
        if section.get("model") is None:
            errors.append(
                "claude_web.model is null. The agent tolerates null (keeps "
                "the session default), but benchmark runs must pin the model "
                f"for the DB identity. Set one of: {', '.join(_CLAUDE_MODELS)}."
            )
        effort = section.get("effort")
        if effort is not None and effort not in _EFFORTS:
            errors.append(
                f"claude_web.effort={effort!r} is not one of "
                f"{', '.join(_EFFORTS)} (or null)."
            )
    elif provider == "chatgpt":
        if not section.get("project_id"):
            errors.append(
                "chatgpt_web.project_id is empty. Copy from "
                "https://chatgpt.com/g/g-p-{id}/project."
            )
        # project_slug is optional — new ChatGPT project URLs have no slug.
        if section.get("agent_mode"):
            errors.append(
                "chatgpt_web.agent_mode=true, but Agent mode no longer "
                "exists in the ChatGPT UI (removed ~mid-2026). Set it to "
                "false and use chatgpt_web.model + chatgpt_web.intelligence."
            )
        gmode = (section.get("mode") or "chat").lower()
        if gmode not in ("chat", "work"):
            errors.append(f"chatgpt_web.mode={gmode!r} is not 'chat' or 'work'.")
        if gmode == "work":
            model = section.get("model")
            if model is not None and model not in _CHATGPT_WORK_MODELS:
                errors.append(
                    f"chatgpt_web.model={model!r} is not a work-mode model "
                    f"({', '.join(_CHATGPT_WORK_MODELS)}) or null."
                )
            w_effort = section.get("effort")
            if w_effort is not None and w_effort not in _WORK_EFFORTS:
                errors.append(
                    f"chatgpt_web.effort={w_effort!r} is not one of "
                    f"{', '.join(_WORK_EFFORTS)} (or null)."
                )
            w_speed = section.get("speed")
            if w_speed is not None and w_speed not in _WORK_SPEEDS:
                errors.append(
                    f"chatgpt_web.speed={w_speed!r} is not one of "
                    f"{', '.join(_WORK_SPEEDS)} (or null = standard)."
                )
            if section.get("intelligence") is not None:
                errors.append(
                    "chatgpt_web.intelligence is set but mode=work — the "
                    "intelligence axis exists only in chat mode. Use "
                    "chatgpt_web.effort (+ speed) for work mode."
                )
        else:
            intel = section.get("intelligence")
            if intel is not None and intel not in _INTELLIGENCE:
                errors.append(
                    f"chatgpt_web.intelligence={intel!r} is not one of "
                    f"{', '.join(_INTELLIGENCE)} (or null)."
                )
            # Legacy one-axis values stay valid: identity has entries for
            # them and the agent routes them to the intelligence axis (with
            # a warning). Only genuinely unknown values are errors.
            _LEGACY_CHATGPT_MODELS = ("instant", "thinking", "pro")
            model = section.get("model")
            if (
                model is not None
                and model not in _CHATGPT_MODELS
                and model not in _LEGACY_CHATGPT_MODELS
            ):
                errors.append(
                    f"chatgpt_web.model={model!r} is not one of "
                    f"{', '.join(_CHATGPT_MODELS)} "
                    f"(legacy: {', '.join(_LEGACY_CHATGPT_MODELS)}) or null."
                )

    # prompts_file must exist and be non-empty — a benchmark run with the
    # wrong (or empty) prompt is worse than one that never starts.
    pf = engine_config.get("prompts_file")
    if pf:
        repo_root = Path(__file__).resolve().parent.parent
        for raw in [pf] if isinstance(pf, str) else pf:
            p = Path(raw)
            if not p.is_absolute():
                cand = repo_root / p
                p = cand if cand.exists() else Path.cwd() / p
            if not p.exists():
                errors.append(f"prompts_file not found: {raw}")
            elif p.stat().st_size == 0:
                errors.append(f"prompts_file is empty: {raw}")
    elif not engine_config.get("prompts"):
        errors.append(
            "No prompts configured — set prompts_file (preferred) or prompts."
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
    solutions = run_dir / "solutions"
    if not solutions.exists():
        return None
    # Must match rename_solution_file's sanitizer; otherwise task names with
    # stripped chars (e.g. '&') fail to match the on-disk filename.
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
        prefix=f"gui_agents_{engine_config.get('task_name', 'task')}_",
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


def _resolve_run_dir(engine_config: dict, provider: str) -> Path:
    section = engine_config.get(f"{provider}_web", {}) or {}
    out = section.get("output", {}) or {}
    base = Path(out.get("base_dir", "."))
    folder_prefix = out.get(
        "folder_prefix",
        {"claude": "claudeGUI", "chatgpt": "chatgptGUI"}.get(provider, "claudeGUI"),
    )
    return base / f"{datetime.now().strftime('%Y%m%d')}_{folder_prefix}"


def _resolve_log_dir(engine_config: dict, provider: str) -> Path:
    section = engine_config.get(f"{provider}_web", {}) or {}
    log_dir = (section.get("logging", {}) or {}).get(
        "log_directory", f"{provider}_web_logs"
    )
    return Path(log_dir)


# Per-provider config keys that the preflight in this file enforces. Keep
# this list in sync with `preflight_check()` below — these are the keys
# that path *requires* the user to set, and the only ones worth scaffolding
# into configs.yaml as explicit null entries.
PROVIDER_REQUIRED_KEYS: dict[str, list[tuple[str, ...]]] = {
    "claude": [("claude_web", "model")],
    "chatgpt": [
        ("chatgpt_web", "project_id"),
    ],
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
    parser = argparse.ArgumentParser(description="gui-agents runner (task-io driven)")
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
            "proceeds even if an earlier attempt exists. Used by the "
            "worker loop to execute one queued task per invocation."
        ),
    )
    args = parser.parse_args()

    run_config_path: Path | None = None
    run_config_is_task_yaml = False
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
            # Task-shaped file: strip reserved task fields (task_name,
            # upload_files, …) and overlay the remaining keys as a project-
            # wide layer. YamlTaskSource then reads the same file for the
            # task definition. There's no separate per-task override layer
            # — everything in this file is run-scoped.
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

    provider = cfg.provider.kind

    engine_script = _REPO_ROOT / "claude_web_agent" / "claude_web_engine.py"
    if not engine_script.exists():
        logger.error(f"Engine not found: {engine_script}")
        return 2

    try:
        source = build_source(cfg)
        sink = build_sink(cfg)
    except ValueError as e:
        # Build failures here are user-facing: empty required fields
        # (database.url, aws.*), unknown source/sink kinds, etc.
        # Swallow the traceback and log the message the class raised.
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

        required = [".".join(p) for p in PROVIDER_REQUIRED_KEYS.get(provider, [])]
        if ensure_overrides_present(
            required, context=f"Preflight for provider {provider!r}"
        ):
            return 0

        # Build + preflight every task BEFORE the user-confirmation prompt.
        # If any task has null configs or missing files, abort here so the
        # user never sees "About to run" for a run that's guaranteed to fail.
        prepared: list[tuple[TaskSpec, dict]] = []
        had_errors = False
        for spec in specs:
            engine_config = build_engine_config(cfg, spec)
            errors = preflight_check(engine_config, provider)
            if errors:
                had_errors = True
                logger.error(
                    f"Preflight failed for task {spec.task_name!r} "
                    f"(provider={provider!r}):"
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

            log_dir = _resolve_log_dir(engine_config, provider)
            run_dir = _resolve_run_dir(engine_config, provider)
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
