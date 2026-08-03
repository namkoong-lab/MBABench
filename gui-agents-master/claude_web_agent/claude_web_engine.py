#!/usr/bin/env python3
"""
Claude Web Engine - Automate tasks through Claude.ai and ChatGPT web interfaces.

This is the main entry point for running tasks through https://claude.ai
or https://chatgpt.com using browser automation.

Workflow:
1. Load config (with hierarchical overrides)
2. Two-tier retry loop:
   a. Pipeline phase: launch browser, navigate, auth, upload files
   b. Agent phase: process prompts, download artifacts, validate
3. Save results (JSON logs + solution files) in date-organized folders

Usage:
    # Standalone
    python claude_web_engine.py --config config.yaml

    # Batch automation
    python claude_web_batch_runner.py --tasks tasks.yaml --template template_claude_web.yaml

    # Non-interactive mode
    python claude_web_engine.py --config config.yaml --no-hold
"""

import argparse
import asyncio
import json
import logging
import re
import shutil
import signal
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from claude_web_agent.claude_web_agent import ClaudeWebAgent
from claude_web_agent.chatgpt_web_agent import ChatGPTWebAgent
from claude_web_agent.web_agent import WebAgent, WebAgentState
from claude_web_agent.browser_manager import WebBrowserManager
from claude_web_agent.completion_logger import CompletionLogger
from claude_web_agent.file_validator import validate_excel_file
from claude_web_agent.task_status import (
    PipelineError,
    TaskStatus,
)

# Initialize logger
logger = logging.getLogger(__name__)

# Global shutdown event
shutdown_event = asyncio.Event()


def _handle_signal(signum, frame):
    """Handle shutdown signals gracefully."""
    try:
        shutdown_event.set()
    except Exception:
        pass
    signal.signal(signal.SIGINT, signal.SIG_DFL)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def create_run_directory(
    base_dir: str | Path, folder_prefix: str = "claudeGUI"
) -> Path:
    """
    Create date-organized output directory.

    Structure:
        {YYYYMMDD}_{folder_prefix}/
        ├── solutions/
        └── json_logs/

    Returns the run_dir Path ({YYYYMMDD}_{folder_prefix}/).
    """
    base_dir = Path(base_dir)
    date_str = datetime.now().strftime("%Y%m%d")
    run_dir = base_dir / f"{date_str}_{folder_prefix}"
    (run_dir / "solutions").mkdir(parents=True, exist_ok=True)
    (run_dir / "json_logs").mkdir(parents=True, exist_ok=True)
    return run_dir


def _sanitize_name(name: str) -> str:
    # Keep in sync with find_solution_file in infra/run.py — the finder
    # must apply the same rule to re-locate the renamed file on disk.
    s = name.replace("/", "-").replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9._-]", "", s)


def rename_solution_file(
    file_path: str | Path,
    task_name: str,
    agent_name: str = "claude_web",
    solution_name: str | None = None,
) -> Path:
    """
    Rename a downloaded solution file.

    If *solution_name* is provided:
        {YYYYMMDD}_{HHMMSS}_{solution_name}_{agent}.xlsx
    Otherwise:
        {YYYYMMDD}_{HHMMSS}_{task_name}_Solution_{agent}_Model.xlsx

    Returns the new file path.
    """
    file_path = Path(file_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if solution_name:
        safe_name = _sanitize_name(solution_name)
        new_name = f"{timestamp}_{safe_name}_{agent_name}{file_path.suffix}"
    else:
        safe_task = _sanitize_name(task_name)
        new_name = (
            f"{timestamp}_{safe_task}_Solution_{agent_name}_Model{file_path.suffix}"
        )

    new_path = file_path.parent / new_name

    # Handle collision — increment counter on the BASE name, not the collided name
    base_stem = new_path.stem
    counter = 1
    while new_path.exists():
        new_path = file_path.parent / f"{base_stem}_{counter}{file_path.suffix}"
        counter += 1

    shutil.move(str(file_path), str(new_path))
    logger.info(f"Renamed solution: {file_path.name} -> {new_path.name}")
    return new_path


def mark_json_deprecated(
    json_path: str | Path, reason: str = "Superseded by later attempt"
):
    """Mark a completion JSON as deprecated by updating it on disk."""
    json_path = Path(json_path)
    if not json_path.exists():
        return
    try:
        with open(json_path) as f:
            data = json.load(f)
        for task in data.get("tasks", []):
            task["deprecated"] = True
            task["deprecated_reason"] = reason
        with open(json_path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Marked as deprecated: {json_path.name}")
    except Exception as e:
        logger.warning(f"Failed to mark JSON deprecated: {e}")


async def _close_stale_pages(pages, keep=None, timeout_s: float = 5.0) -> None:
    """Close every page in `pages` except `keep`, bounding each close().

    Pages with a hung renderer or a beforeunload dialog can make
    ``page.close()`` block forever. We cap each close at ``timeout_s`` and
    move on — the page object becomes orphaned but Chrome usually reaps it
    on its own, and the run can proceed.
    """
    for stale in list(pages):
        if stale is keep:
            continue
        try:
            await asyncio.wait_for(
                stale.close(run_before_unload=False), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            url = ""
            try:
                url = stale.url
            except Exception:
                pass
            logger.warning(
                f"Stale page close timed out after {timeout_s}s; skipping "
                f"(url={url!r})"
            )
        except Exception:
            pass


async def _cleanup_browser(browser_mgr, context, browser, page):
    """Best-effort browser cleanup.

    In CDP mode, navigate the page to about:blank instead of closing it
    to prevent Chrome from exiting when its last tab is closed.
    """
    try:
        if page:
            if browser_mgr and browser_mgr.is_cdp_mode():
                # Keep at least one tab alive so Chrome doesn't exit
                await page.goto("about:blank", wait_until="commit", timeout=5000)
            else:
                await page.close()
    except Exception:
        pass
    try:
        if browser_mgr and context and browser:
            await browser_mgr.close_browser(context, browser)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Logging setup + config loading (unchanged)
# ---------------------------------------------------------------------------


def setup_logging(config: dict, name: str, task_name: str = None) -> tuple:
    """Setup logging with optional file output."""
    _, agent_config = get_provider_config(config)
    log_config = agent_config.get("logging", {})
    log_level = getattr(logging, log_config.get("level", "INFO").upper())
    save_to_file = log_config.get("save_to_file", True)
    log_directory = log_config.get("log_directory", "claude_web_logs")

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    root_logger.addHandler(console_handler)

    log_file_path = None

    if save_to_file:
        # Create log directory
        log_dir = Path(log_directory)
        log_dir.mkdir(parents=True, exist_ok=True)

        # Create log file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_task_name = (
            task_name.replace("/", "_").replace("\\", "_") if task_name else "unknown"
        )
        log_filename = f"claude_web_{timestamp}_{safe_task_name}.log"
        log_file_path = log_dir / log_filename

        file_handler = logging.FileHandler(log_file_path)
        file_handler.setLevel(log_level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        root_logger.addHandler(file_handler)

    return logging.getLogger(name), log_file_path


def resolve_prompts(config: dict) -> dict:
    """Expand ``prompts_file`` into the ``prompts`` list, in place.

    Benchmark prompts are long (~13k chars) and are the experimental control:
    duplicating them across template YAMLs invites silent drift between runs.
    ``prompts_file`` points at one or more text files, each holding exactly
    one prompt, so every config references a single source of truth.

    Paths resolve relative to the repo root, then CWD. ``prompts_file`` wins
    over an inline ``prompts`` list; a missing file is fatal (running the
    benchmark with the wrong prompt is worse than not running it).
    """
    spec = config.get("prompts_file")
    if not spec:
        return config

    paths = [spec] if isinstance(spec, str) else list(spec)
    repo_root = Path(__file__).resolve().parent.parent
    resolved: list[str] = []
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            candidate = repo_root / p
            p = candidate if candidate.exists() else Path.cwd() / p
        if not p.exists():
            raise FileNotFoundError(
                f"prompts_file not found: {raw!r} (looked in {repo_root} and "
                f"{Path.cwd()}). Refusing to run with the wrong prompt."
            )
        resolved.append(p.read_text())

    if config.get("prompts"):
        logger.warning(
            "Both prompts_file and prompts are set — prompts_file wins "
            "(%d prompt(s) from %s)",
            len(resolved),
            ", ".join(str(x) for x in paths),
        )
    config["prompts"] = resolved
    logger.info(
        "Loaded %d prompt(s) from prompts_file (%d chars total)",
        len(resolved),
        sum(len(p) for p in resolved),
    )
    return config


def load_config(config_path: str) -> dict:
    """Load configuration from YAML file."""
    import yaml

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # Handle template nesting
    if "template" in config:
        config = config["template"]

    return resolve_prompts(config)


def get_provider_config(config: dict) -> tuple[str, dict]:
    """
    Determine provider from config and return (provider_key, agent_config).
    provider_key is 'claude_web' or 'chatgpt_web'.
    """
    agent_type = config.get("agent_type", "claude_web")
    if agent_type == "chatgpt_web":
        return "chatgpt_web", config.get("chatgpt_web", {})
    return "claude_web", config.get("claude_web", {})


def create_agent(
    provider_key: str, page, config: dict, shutdown_event=None, completion_logger=None
) -> WebAgent:
    """Factory: create the right WebAgent subclass based on provider."""
    if provider_key == "chatgpt_web":
        return ChatGPTWebAgent(
            page=page,
            config=config,
            shutdown_event=shutdown_event,
            completion_logger=completion_logger,
        )
    return ClaudeWebAgent(
        page=page,
        config=config,
        shutdown_event=shutdown_event,
        completion_logger=completion_logger,
    )


PROVIDER_DEFAULTS = {
    "claude_web": {
        "folder_prefix": "claudeGUI",
        "agent_name": "claude_web",
    },
    "chatgpt_web": {
        "folder_prefix": "chatgptGUI",
        "agent_name": "chatgpt_web",
    },
}


# ---------------------------------------------------------------------------
# Main automation with two-tier retry loop
# ---------------------------------------------------------------------------


async def run_automation(config: dict) -> bool:
    """
    Main automation entry point with two-tier retry loop.

    Phase 1 (Pipeline): Browser launch, navigation, auth, file upload.
        Failures here are infrastructure issues — no JSON created, no agent_attempts.

    Phase 2 (Agent): Prompt processing, download, validation.
        Failures here are the agent's fault — JSON created, agent_attempts incremented.

    Args:
        config: Configuration dictionary

    Returns:
        True if automation succeeded
    """
    # ---- Provider detection ----
    provider_key, agent_config = get_provider_config(config)
    no_hold = agent_config.get("runtime", {}).get("no_hold", False)
    provider_defaults = PROVIDER_DEFAULTS.get(
        provider_key, PROVIDER_DEFAULTS["claude_web"]
    )
    logger.info(f"Provider: {provider_key}")

    # ---- Config extraction ----
    retry_config = agent_config.get("retry", {})
    output_config = agent_config.get("output", {})
    session_config = agent_config.get("session", {})

    max_agent_attempts = retry_config.get("max_agent_attempts", 3)
    max_total_attempts = retry_config.get("max_total_attempts", 10)
    max_sec_per_task = retry_config.get(
        "max_sec_per_task",
        agent_config.get("max_sec_per_task", 0),
    )
    max_sec_per_attempt = retry_config.get("max_sec_per_attempt", 1800)
    sleep_between_retries = retry_config.get("sleep_between_retries", 5)

    base_dir = Path(output_config.get("base_dir", "."))
    folder_prefix = output_config.get(
        "folder_prefix", provider_defaults["folder_prefix"]
    )

    agent_name = session_config.get("agent_name", provider_defaults["agent_name"])
    prompt_version = session_config.get(
        "prompt_version", config.get("prompt_version", 1)
    )

    task_id = config.get("task_id")
    task_name = config.get("task_name", "unnamed_task")
    task_source = config.get("task_source", "claude_web")
    solution_name = config.get("solution_name")

    # File upload: prefer "upload_files"; "files_to_upload" is accepted as an alias
    files_to_upload = config.get("upload_files", config.get("files_to_upload", []))

    # Resolve relative paths using local_files_base if provided
    local_files_base = config.get("local_files_base")
    if local_files_base and files_to_upload:
        base = Path(local_files_base)
        resolved = []
        for f in files_to_upload:
            p = Path(f)
            if not p.is_absolute():
                p = (base / p).resolve()
            resolved.append(str(p))
        files_to_upload = resolved

    if max_sec_per_task > 0:
        logger.info(f"Task timeout: {max_sec_per_task}s ({max_sec_per_task // 60}m)")
    else:
        logger.info("No timeout (unlimited runtime)")

    logger.info(f"Task: {task_name} (ID: {task_id})")
    logger.info(f"Source: {task_source}")
    logger.info(
        f"Retry config: max_agent={max_agent_attempts}, max_total={max_total_attempts}"
    )

    # ---- Create date-organized output dirs ----
    run_dir = create_run_directory(base_dir, folder_prefix)
    solutions_dir = run_dir / "solutions"
    json_logs_dir = run_dir / "json_logs"
    logger.info(f"Output directory: {run_dir}")

    # ---- Retry loop ----
    agent_attempts = 0
    total_attempts = 0
    agent_json_paths: list[Path] = []
    task_success = False

    while True:
        # Check termination conditions
        if total_attempts >= max_total_attempts:
            logger.error(f"Max total attempts ({max_total_attempts}) exhausted")
            break
        if agent_attempts >= max_agent_attempts:
            logger.error(f"Max agent attempts ({max_agent_attempts}) exhausted")
            break
        if shutdown_event.is_set():
            logger.warning("Shutdown signal received, exiting retry loop")
            break

        total_attempts += 1
        logger.info(f"\n{'=' * 60}")
        logger.info(
            f"ATTEMPT {total_attempts} (agent_attempts={agent_attempts}/{max_agent_attempts})"
        )
        logger.info(f"{'=' * 60}")

        browser = None
        context = None
        page = None
        browser_mgr = None
        completion_logger = None

        # =====================================================================
        # PHASE 1: Pipeline (no JSON created, no agent_attempts counted)
        # =====================================================================
        try:
            browser_mgr = WebBrowserManager(config)
            playwright_ctx = async_playwright()
            playwright = await playwright_ctx.__aenter__()

            try:
                browser, context = await browser_mgr.launch_browser(playwright)
                # NOTE: Do NOT call set_viewport_size on CDP pages — it uses
                # Emulation.setDeviceMetricsOverride which crashes ChatGPT tabs.
                # For CDP mode, create new page first before closing stale ones
                # to avoid Chrome having zero targets (which breaks new_page).
                if browser_mgr.is_cdp_mode():
                    page = await context.new_page()
                    logger.info("Created new browser page (CDP mode)")
                    await _close_stale_pages(context.pages, keep=page)
                else:
                    await _close_stale_pages(context.pages)
                    page = await context.new_page()
                    logger.info("Created new browser page")

                agent = create_agent(
                    provider_key,
                    page=page,
                    config=config,
                    shutdown_event=shutdown_event,
                    completion_logger=None,
                )

                # Navigate
                provider_name = (
                    "ChatGPT" if provider_key == "chatgpt_web" else "Claude.ai"
                )
                if not await agent.navigate_to_new_chat():
                    raise PipelineError(
                        TaskStatus.NAVIGATION_FAILED,
                        f"Failed to navigate to {provider_name}",
                    )

                # Auth check
                state = await agent.get_state()
                logger.info(f"{provider_name} state: {state.value}")

                if state == WebAgentState.AUTH_REQUIRED:
                    logger.info(
                        "Authentication required — waiting for login (max 5 min)..."
                    )
                    login_timeout = 300
                    elapsed = 0
                    while elapsed < login_timeout:
                        await asyncio.sleep(10)
                        elapsed += 10
                        # After auth redirect, try lightweight navigation back
                        current_url = page.url
                        if (
                            "chatgpt.com" not in current_url
                            and "claude.ai" not in current_url
                        ):
                            if elapsed % 30 == 0:
                                logger.info(
                                    f"Still on auth page ({current_url[:60]}...), retrying navigation..."
                                )
                            try:
                                await page.goto(
                                    (
                                        agent.project_url
                                        if hasattr(agent, "project_url")
                                        else "https://chatgpt.com"
                                    ),
                                    wait_until="domcontentloaded",
                                    timeout=15000,
                                )
                                await page.wait_for_timeout(2000)
                            except Exception:
                                pass
                        state = await agent.get_state()
                        if state == WebAgentState.READY:
                            logger.info("Login successful!")
                            await browser_mgr.save_auth_state(context)
                            break
                        if elapsed % 30 == 0:
                            logger.info(
                                f"Still waiting for login... ({elapsed}s / {login_timeout}s)"
                            )

                    if state != WebAgentState.READY:
                        raise PipelineError(TaskStatus.AUTH_FAILED, "Login timeout")

                if state == WebAgentState.RATE_LIMITED:
                    raise PipelineError(
                        TaskStatus.RATE_LIMITED, "Rate limited before prompts"
                    )

                # Agent mode is now enabled inside submit_prompt() —
                # after files are attached and prompt is typed, before send.

                # Upload files (in Phase 1 so they're ready for Phase 2)
                if files_to_upload:
                    logger.info(f"Uploading {len(files_to_upload)} file(s)...")
                    if not await agent.upload_files(files_to_upload):
                        raise PipelineError(
                            TaskStatus.UPLOAD_FAILED, "File upload failed"
                        )

            except PipelineError:
                # Close playwright before re-raising
                await _cleanup_browser(browser_mgr, context, browser, page)
                await playwright_ctx.__aexit__(None, None, None)
                raise

        except PipelineError as e:
            logger.warning(f"Pipeline failure ({e.status.value}): {e}")
            logger.info(f"Retrying in {sleep_between_retries}s...")
            await asyncio.sleep(sleep_between_retries)
            continue

        except Exception as e:
            logger.error(f"Unexpected Phase 1 error: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            logger.info(f"Retrying in {sleep_between_retries}s...")
            await asyncio.sleep(sleep_between_retries)
            continue

        # =====================================================================
        # PHASE 2: Agent (JSON created, attempt counted)
        # =====================================================================
        try:
            # Create completion logger for this attempt
            completion_logger = CompletionLogger(
                log_dir=str(json_logs_dir),
                task_identifier=task_name,
                agent_name=agent_name,
                prompt_version=prompt_version,
                task_source=task_source,
            )
            completion_logger.start_task(task_name, attempt_number=total_attempts)

            # Wire logger into agent
            agent.completion_logger = completion_logger

            # Set up per-attempt timeout
            attempt_timed_out = False

            async def _attempt_timeout_guard():
                nonlocal attempt_timed_out
                effective_timeout = max_sec_per_attempt
                if max_sec_per_task > 0:
                    effective_timeout = min(effective_timeout, max_sec_per_task)
                if effective_timeout > 0:
                    await asyncio.sleep(effective_timeout)
                    attempt_timed_out = True
                    shutdown_event.set()

            guard_task = asyncio.create_task(_attempt_timeout_guard())

            try:
                # Process prompts (files already uploaded in Phase 1)
                prompt_success = await agent.process_all_prompts(files_to_upload=[])

                if not prompt_success:
                    if attempt_timed_out:
                        status = TaskStatus.TIMEOUT
                    else:
                        status = TaskStatus.PROMPT_FAILED
                    agent_attempts += 1
                    completion_logger.end_task(status)
                    agent_json_paths.append(completion_logger.session_file)
                    logger.warning(f"Agent failure: {status.value}")

                    # Best-effort archival download
                    try:
                        await agent.download_all_artifacts(
                            download_dir=str(solutions_dir)
                        )
                    except Exception:
                        pass

                    try:
                        await _cleanup_browser(browser_mgr, context, browser, page)
                        await playwright_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass

                    await asyncio.sleep(sleep_between_retries)
                    continue

                # Prompt succeeded — download artifacts
                CONTINUE_PROMPT = (
                    "Continue. Please complete all remaining steps and provide "
                    "the finished Excel file for download when done."
                )
                MAX_CONTINUE_ATTEMPTS = 5

                downloaded_files = []
                excel_files = []

                for continue_attempt in range(MAX_CONTINUE_ATTEMPTS + 1):
                    if continue_attempt == 0:
                        logger.info("All prompts completed! Downloading artifacts...")
                    else:
                        logger.info(
                            f"No Excel artifacts yet — sending 'Continue' "
                            f"({continue_attempt}/{MAX_CONTINUE_ATTEMPTS})"
                        )
                        if not await agent.submit_prompt(
                            CONTINUE_PROMPT, prompt_number=continue_attempt + 1
                        ):
                            logger.error("Failed to submit continue prompt")
                            break
                        response = await agent.wait_for_response(
                            prompt_number=continue_attempt + 1
                        )
                        if response is None:
                            logger.warning("No response to continue prompt")
                            break

                    downloaded_files = await agent.download_all_artifacts(
                        download_dir=str(solutions_dir)
                    )
                    if downloaded_files:
                        excel_files = [
                            f
                            for f in downloaded_files
                            if f.lower().endswith((".xlsx", ".xls"))
                        ]
                        if excel_files:
                            break

                    # Download controls were on the page but retrieval
                    # failed — the model already FINISHED. A "Continue"
                    # prompt would just spam a completed model; fail the
                    # attempt as a download problem instead.
                    if getattr(agent, "last_download_saw_buttons", False):
                        logger.warning(
                            "Download buttons visible but no file retrieved "
                            "— skipping 'Continue' (retrieval failure, not "
                            "an incomplete model)"
                        )
                        break

                if not downloaded_files or not excel_files:
                    agent_attempts += 1
                    completion_logger.end_task(TaskStatus.DOWNLOAD_FAILED)
                    agent_json_paths.append(completion_logger.session_file)
                    logger.warning(
                        f"Download failed after {min(continue_attempt + 1, MAX_CONTINUE_ATTEMPTS + 1)} "
                        f"attempt(s) — no Excel artifacts retrieved"
                    )

                    try:
                        await _cleanup_browser(browser_mgr, context, browser, page)
                        await playwright_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    await asyncio.sleep(sleep_between_retries)
                    continue

                validation_failed = False
                for fpath in excel_files:
                    is_valid, val_status, val_msg = validate_excel_file(fpath)
                    if not is_valid:
                        agent_attempts += 1
                        completion_logger.end_task(val_status)
                        agent_json_paths.append(completion_logger.session_file)
                        logger.warning(
                            f"Validation failed ({val_status.value}): {val_msg}"
                        )
                        validation_failed = True
                        break

                if validation_failed:
                    try:
                        await _cleanup_browser(browser_mgr, context, browser, page)
                        await playwright_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    await asyncio.sleep(sleep_between_retries)
                    continue

                # Rename solution files
                renamed_files = []
                for fpath in downloaded_files:
                    if fpath.lower().endswith((".xlsx", ".xls")):
                        new_path = rename_solution_file(
                            fpath, task_name, agent_name, solution_name
                        )
                        renamed_files.append(str(new_path))
                    else:
                        renamed_files.append(fpath)

                # SUCCESS
                agent_attempts += 1
                completion_logger.end_task(TaskStatus.SUCCESS)
                agent_json_paths.append(completion_logger.session_file)
                logger.info("Task completed successfully!")

                # Save conversation history
                history = await agent.get_conversation_history()
                default_log_dir = (
                    "chatgpt_web_logs"
                    if provider_key == "chatgpt_web"
                    else "claude_web_logs"
                )
                log_dir = Path(
                    agent_config.get("logging", {}).get(
                        "log_directory", default_log_dir
                    )
                )
                history_dir = log_dir / "conversations"
                history_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                history_file = (
                    history_dir / f"conversation_{timestamp}_{task_name}.json"
                )
                with open(history_file, "w") as f:
                    json.dump(
                        {
                            "task_name": task_name,
                            "task_source": task_source,
                            "timestamp": datetime.now().isoformat(),
                            "messages": history,
                        },
                        f,
                        indent=2,
                    )
                logger.info(f"Saved conversation to: {history_file}")

                # Hold browser open unless --no-hold
                if not no_hold:
                    logger.info("Browser staying open for inspection...")
                    logger.info("Press Ctrl+C to exit")
                    while not shutdown_event.is_set():
                        await asyncio.sleep(1)

                await _cleanup_browser(browser_mgr, context, browser, page)
                await playwright_ctx.__aexit__(None, None, None)
                task_success = True
                break  # Done

            finally:
                guard_task.cancel()
                # Reset shutdown for potential next attempt
                if attempt_timed_out:
                    shutdown_event.clear()

        except Exception as e:
            logger.error(f"Unexpected error in Phase 2: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            agent_attempts += 1
            if completion_logger and completion_logger.current_task:
                completion_logger.end_task(TaskStatus.UNKNOWN)
                agent_json_paths.append(completion_logger.session_file)
            await _cleanup_browser(browser_mgr, context, browser, page)
            try:
                await playwright_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            await asyncio.sleep(sleep_between_retries)
            continue

    # ---- Post-loop: deprecate earlier agent JSONs ----
    if len(agent_json_paths) > 1:
        for path in agent_json_paths[:-1]:
            mark_json_deprecated(path, "Superseded by later attempt")

    if not task_success:
        logger.error(
            f"Task failed after {total_attempts} total attempts "
            f"({agent_attempts} agent attempts)"
        )

    return task_success


def main():
    """Main entry point."""
    # CLI Arguments
    parser = argparse.ArgumentParser(
        description="Claude Web Engine - Automate tasks through Claude.ai",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode
  python claude_web_engine.py --config config.yaml

  # Batch mode
  python claude_web_engine.py --config config.yaml --no-hold

  # With timeout
  python claude_web_engine.py --config config.yaml --no-hold --max-runtime 3600
        """,
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file",
    )
    parser.add_argument(
        "--no-hold",
        action="store_true",
        help="Exit immediately after completion",
    )
    parser.add_argument(
        "--max-runtime",
        type=int,
        default=0,
        help="Maximum runtime in seconds (0 = unlimited)",
    )
    args = parser.parse_args()

    # Signal handlers
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        # Load .env
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)

        # Load config
        config_path = Path(args.config)
        if not config_path.is_absolute():
            # Try current directory first
            if (Path.cwd() / config_path).exists():
                config_path = Path.cwd() / config_path
            # Then try relative to script
            elif (Path(__file__).parent / config_path).exists():
                config_path = Path(__file__).parent / config_path

        if not config_path.exists():
            print(f"Config file not found: {config_path}")
            sys.exit(1)

        config = load_config(str(config_path))

        # Apply CLI overrides
        if args.no_hold:
            for key in ("claude_web", "chatgpt_web"):
                if key in config:
                    config[key].setdefault("runtime", {})["no_hold"] = True

        if args.max_runtime > 0:
            if "claude_web" not in config:
                config["claude_web"] = {}
            config["claude_web"]["max_sec_per_task"] = args.max_runtime

        # Setup logging
        task_name = config.get("task_name", "unknown_task")
        global logger
        logger, _ = setup_logging(config, __name__, task_name=task_name)

        # Run automation
        logger.info("=" * 60)
        logger.info("Claude Web Engine Starting")
        logger.info("=" * 60)
        logger.info(f"Config: {config_path}")
        logger.info(f"Task: {task_name}")

        success = asyncio.run(run_automation(config))

        if success:
            print("\nSUCCESS")
        else:
            print("\nFAILED")

        sys.exit(0 if success else 1)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Script failed: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
