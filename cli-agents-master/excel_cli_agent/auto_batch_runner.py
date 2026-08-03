#!/usr/bin/env python3
"""
Auto Batch Runner for Excel CLI Agent

Automated pipeline that handles the full lifecycle:
  DB lookup -> trial count check -> S3 download -> task execution -> S3 upload -> DB entry

Extends BatchRunner with auto-discovery, S3 workspace setup, and result upload.
"""

import json
import os
import shutil
import time
import traceback
import yaml
import boto3
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, date

from .batch_runner import BatchRunner, WorkspaceConfig, WorkspaceResult, BatchResult

from .db.database import SessionLocal
from .db.models import Task, TaskAttempt
from .prompt_versions import PROMPTS_DIR, PROMPT_VERSIONS, DEFAULT_PROMPT_VERSION, parse_prompt_version

# Resolved prompt paths (set by load_config based on prompt_version)
SYSTEM_PROMPT_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["system"]
TASK_TEMPLATE_FMWC_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["fmwc"]
TASK_TEMPLATE_WSP_PATH: Path = PROMPTS_DIR / PROMPT_VERSIONS[DEFAULT_PROMPT_VERSION]["wsp"]


@dataclass
class TaskInfo:
    """Information about a task from the database"""
    task_id: int
    task_name: str
    task_source: str
    task_starting_files: List[str] = field(default_factory=list)


def _normalize_task_name(name: str) -> str:
    """Normalize task name: replace spaces with underscores for DB lookup."""
    return name.replace(' ', '_')



class AutoBatchRunner(BatchRunner):
    """Automated batch runner with DB-driven task discovery, S3 download/upload, and trial management."""

    def __init__(self, config_path: str, server_path: str, api_key: str,
                 custom_reasoning: bool = False, enable_langfuse: bool = False):
        super().__init__(config_path, server_path, api_key, custom_reasoning, enable_langfuse)
        self._s3_client = None
        self._prompt_s3_uris: Optional[List[str]] = None
        self._s3_bucket = "mbabench"

    @property
    def s3_client(self):
        if self._s3_client is None:
            self._s3_client = boto3.client("s3")
        return self._s3_client

    def _s3_agent_prefix(self) -> str:
        """Build the S3 prefix: BizbenchV1/attempts/{model}_openpyxl"""
        model = self.config['model']
        return f"BizbenchV1/attempts/{model}_openpyxl"

    def _s3_task_prefix(self, task_info: TaskInfo) -> str:
        """Build per-task S3 prefix: {agent_prefix}/task_source={src}/task_id={id}"""
        return f"{self._s3_agent_prefix()}/task_source={task_info.task_source}/task_id={task_info.task_id}"

    def load_config(self) -> Dict[str, Any]:
        """Load and validate YAML configuration for auto mode.

        Relaxes required fields - no task_template or workspaces needed.
        """
        print(f"📋 Loading auto batch configuration from {self.config_path}")

        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Validate required fields for auto mode
        required_fields = ['batch_name', 'model']
        for fld in required_fields:
            if fld not in config:
                raise ValueError(f"Missing required field in config: {fld}")

        if not config.get('auto_mode'):
            raise ValueError("auto_mode must be true for AutoBatchRunner")

        if not config.get('workspace_base_dir'):
            raise ValueError("workspace_base_dir is required in auto_mode")

        # Must have either tasks, task_ids, or task_filter
        if 'tasks' not in config and 'task_ids' not in config and 'task_filter' not in config:
            raise ValueError(
                "Either 'tasks' (list of task names), 'task_ids' (list of task ids), "
                "or 'task_filter' is required"
            )

        # Set defaults
        config.setdefault('verbose', False)
        config.setdefault('max_iterations', 30)
        config.setdefault('batch_size', 1)
        config.setdefault('snapshot_iterations', False)
        config.setdefault('max_trials', 7)
        config.setdefault('trials_since', date.today().isoformat())
        # When true, a task with a non-failed attempt (for this agent, since
        # trials_since) is skipped even if trials remain — so a resumed batch
        # never re-runs tasks that already succeeded. Default false preserves
        # the historical trial-count-only behavior.
        config.setdefault('skip_if_succeeded', False)

        # Default agent_folder from model name
        if 'agent_folder' not in config:
            config['agent_folder'] = f"openpyxl_{config['model']}"

        # Resolve prompt version from config (or default)
        global SYSTEM_PROMPT_PATH, TASK_TEMPLATE_FMWC_PATH, TASK_TEMPLATE_WSP_PATH
        prompt_ver = config.get('prompt_version', DEFAULT_PROMPT_VERSION)
        if prompt_ver not in PROMPT_VERSIONS:
            raise ValueError(f"Unknown prompt_version '{prompt_ver}'. Available: {list(PROMPT_VERSIONS.keys())}")
        ver_files = PROMPT_VERSIONS[prompt_ver]
        SYSTEM_PROMPT_PATH = PROMPTS_DIR / ver_files["system"]
        TASK_TEMPLATE_FMWC_PATH = PROMPTS_DIR / ver_files["fmwc"]
        TASK_TEMPLATE_WSP_PATH = PROMPTS_DIR / ver_files["wsp"]

        # Set versioned system prompt path for TaskExecutor
        config['system_prompt_path'] = str(SYSTEM_PROMPT_PATH)

        self.config = config

        print(f"✅ Configuration loaded: {config['batch_name']}")
        print(f"   Model: {config['model']}")
        print(f"   Agent folder: {config['agent_folder']}")
        print(f"   Prompt version: {prompt_ver}")
        print(f"   Max iterations: {config['max_iterations']}")
        print(f"   Max trials: {config['max_trials']}")
        print(f"   Trials since: {config['trials_since']}")
        print(f"   Workspace base: {config['workspace_base_dir']}")

        return config

    def resolve_tasks(self) -> List[TaskInfo]:
        """Query DB for tasks to process based on config."""
        db = SessionLocal()
        try:
            return self._resolve_tasks_impl(db)
        finally:
            db.close()

    def _resolve_tasks_impl(self, db) -> List[TaskInfo]:
        tasks_result = []

        if 'task_ids' in self.config:
            # Explicit task ids mode — preserves the given order (e.g. a
            # lightest-to-heaviest ranking).
            task_ids = self.config['task_ids']
            print(f"\n🔍 Resolving {len(task_ids)} explicit task id(s) from database...")
            for tid in task_ids:
                task = db.query(Task).filter(Task.id == tid).first()
                if task is None:
                    print(f"  ❌ id {tid} -> NOT FOUND in DB")
                    continue
                if task.deprecated:
                    print(f"  ⏭️  id {tid} ({task.task_name}) -> deprecated, skipping")
                    continue
                tasks_result.append(TaskInfo(
                    task_id=task.id,
                    task_name=task.task_name,
                    task_source=task.task_source,
                    task_starting_files=task.task_starting_files or [],
                ))
                print(f"  ✅ id {tid} -> {task.task_name} ({task.task_source})")

        elif 'tasks' in self.config:
            # Explicit task names mode
            task_names = self.config['tasks']
            print(f"\n🔍 Resolving {len(task_names)} explicit task(s) from database...")

            for name in task_names:
                task = self._find_task_by_name(db, name)
                if task:
                    tasks_result.append(TaskInfo(
                        task_id=task.id,
                        task_name=task.task_name,
                        task_source=task.task_source,
                        task_starting_files=task.task_starting_files or [],
                    ))
                    print(f"  ✅ {name} -> ID {task.id} ({task.task_source})")
                else:
                    print(f"  ❌ {name} -> NOT FOUND in DB")

        elif 'task_filter' in self.config:
            # Auto-discovery mode
            tf = self.config['task_filter']
            print(f"\n🔍 Auto-discovering tasks from database...")

            query = db.query(Task).filter(Task.deprecated == False)  # noqa: E712

            # Filter by task_source
            task_source = tf.get('task_source')
            if task_source:
                query = query.filter(Task.task_source == task_source)
                print(f"  Filter: task_source = {task_source}")

            all_tasks = query.all()
            print(f"  Found {len(all_tasks)} eligible tasks in DB")

            # Filter for missing_for_model: only tasks where trial count < max_trials
            if tf.get('missing_for_model'):
                agent_folder = self.config['agent_folder']
                trials_since = self.config['trials_since']
                max_trials = self.config['max_trials']

                for task in all_tasks:
                    trial_count = self._get_trial_count(db, task.id, agent_folder, trials_since)
                    if trial_count < max_trials:
                        tasks_result.append(TaskInfo(
                            task_id=task.id,
                            task_name=task.task_name,
                            task_source=task.task_source,
                            task_starting_files=task.task_starting_files or [],
                        ))

                print(f"  After trial filter (< {max_trials} trials since {trials_since}): {len(tasks_result)} tasks")
            else:
                for task in all_tasks:
                    tasks_result.append(TaskInfo(
                        task_id=task.id,
                        task_name=task.task_name,
                        task_source=task.task_source,
                        task_starting_files=task.task_starting_files or [],
                    ))

        # Explicit skip list — tasks intentionally deferred (e.g. starting
        # files too large for the model's input context). See SKIPPED_TASKS.md.
        skip_ids = set(self.config.get('skip_task_ids') or [])
        if skip_ids:
            kept = [t for t in tasks_result if t.task_id not in skip_ids]
            dropped = sorted({t.task_id for t in tasks_result} & skip_ids)
            if dropped:
                print(f"⏭️  Skipping {len(dropped)} task id(s) via skip_task_ids: {dropped}")
            tasks_result = kept

        print(f"📋 Total tasks to process: {len(tasks_result)}")
        return tasks_result

    def _find_task_by_name(self, db, name: str) -> Optional[Task]:
        """Find a task by name, trying normalized variants."""
        normalized = _normalize_task_name(name)

        # Try normalized first (canonical)
        if normalized != name:
            task = db.query(Task).filter(
                Task.task_name == normalized,
                Task.deprecated == False  # noqa: E712
            ).first()
            if task:
                return task

        # Try exact
        task = db.query(Task).filter(
            Task.task_name == name,
            Task.deprecated == False  # noqa: E712
        ).first()
        if task:
            return task

        # Fallback: any match including deprecated
        task = db.query(Task).filter(Task.task_name == name).first()
        if task:
            return task
        if normalized != name:
            task = db.query(Task).filter(Task.task_name == normalized).first()
        return task

    def _get_trial_count(self, db, task_id: int, agent_folder: str, trials_since: str) -> int:
        """Count non-deprecated attempts for a task+model since a given date."""
        from sqlalchemy import func as sa_func
        count = db.query(sa_func.count(TaskAttempt.id)).filter(
            TaskAttempt.task_id == task_id,
            TaskAttempt.agent_model_name == agent_folder,
            TaskAttempt.deprecated == False,  # noqa: E712
            TaskAttempt.created_at >= trials_since,
        ).scalar()
        return count or 0

    def get_trial_count(self, task_info: TaskInfo) -> int:
        """Public interface: get trial count for a task."""
        db = SessionLocal()
        try:
            return self._get_trial_count(
                db, task_info.task_id,
                self.config['agent_folder'],
                self.config['trials_since'],
            )
        finally:
            db.close()

    def _has_success(self, task_info: TaskInfo) -> bool:
        """True if a non-failed, non-deprecated attempt exists for this
        task+agent since trials_since."""
        db = SessionLocal()
        try:
            row = db.query(TaskAttempt.id).filter(
                TaskAttempt.task_id == task_info.task_id,
                TaskAttempt.agent_model_name == self.config['agent_folder'],
                TaskAttempt.deprecated == False,  # noqa: E712
                TaskAttempt.agent_failed.isnot(True),
                TaskAttempt.created_at >= self.config['trials_since'],
            ).first()
            return row is not None
        finally:
            db.close()

    def should_skip(self, task_info: TaskInfo) -> bool:
        """Check if task should be skipped (already succeeded, or max trials)."""
        if self.config.get('skip_if_succeeded') and self._has_success(task_info):
            print(f"  ⏭️  {task_info.task_name}: already has a successful attempt, skipping")
            return True

        trial_count = self.get_trial_count(task_info)
        max_trials = self.config['max_trials']

        if trial_count >= max_trials:
            print(f"  ⏭️  {task_info.task_name}: {trial_count}/{max_trials} trials exhausted, skipping")
            return True

        print(f"  📊 {task_info.task_name}: trial {trial_count + 1}/{max_trials}")
        return False

    def setup_workspace(self, task_info: TaskInfo) -> str:
        """Create workspace directory and download S3 starting files."""
        base_dir = Path(self.config['workspace_base_dir']).expanduser()
        # Use task_name with spaces replaced by underscores for folder names
        # Append timestamp+PID to avoid collisions when multiple processes run concurrently
        folder_name = task_info.task_name.replace(' ', '_')
        run_id = f"{int(time.time())}_{os.getpid()}"
        workspace = base_dir / f"{folder_name}_{run_id}"
        workspace.mkdir(parents=True, exist_ok=True)

        print(f"  📁 Workspace: {workspace}")

        if not task_info.task_starting_files:
            print(f"  ⚠️  No starting files for task {task_info.task_name}")
            return str(workspace)

        for s3_uri in task_info.task_starting_files:
            # Parse s3://bucket/key
            if not s3_uri.startswith("s3://"):
                print(f"  ⚠️  Invalid S3 URI: {s3_uri}")
                continue

            parts = s3_uri[5:].split("/", 1)
            if len(parts) != 2:
                print(f"  ⚠️  Invalid S3 URI format: {s3_uri}")
                continue

            bucket = parts[0]
            key = parts[1]
            filename = key.split("/")[-1]
            local_path = workspace / filename

            if local_path.exists():
                print(f"  ✅ Already exists: {filename}")
                continue

            try:
                print(f"  ⬇️  Downloading: {filename}")
                self.s3_client.download_file(bucket, key, str(local_path))
                print(f"  ✅ Downloaded: {filename} ({local_path.stat().st_size:,} bytes)")
            except Exception as e:
                print(f"  ❌ Failed to download {filename}: {e}")

        return str(workspace)

    def cleanup_workspace(self, workspace_path: str):
        """Delete entire workspace folder after results are uploaded to S3."""
        if self.config.get('cleanup_workspace', True) is False:
            print(f"  📂 Workspace preserved: {workspace_path}")
            return
        try:
            shutil.rmtree(workspace_path)
            print(f"  🧹 Cleaned up workspace: {workspace_path}")
        except Exception as e:
            print(f"  ⚠️  Cleanup failed for {workspace_path}: {e}")

    def select_task_template(self, task_source: str) -> str:
        """Load task_template from versioned txt file based on task_source."""
        template_path = TASK_TEMPLATE_WSP_PATH if task_source == "wsp" else TASK_TEMPLATE_FMWC_PATH
        if not template_path.exists():
            raise FileNotFoundError(f"Task template not found: {template_path}")
        return template_path.read_text(encoding='utf-8')

    def upload_prompts(self) -> List[str]:
        """Upload versioned prompt snapshots to S3. Called once at batch start.

        Path: BizbenchV1/prompts/{model}_openpyxl/{timestamp}_{filename}
        """
        if self._prompt_s3_uris is not None:
            return self._prompt_s3_uris

        agent_prefix = self._s3_agent_prefix()  # BizbenchV1/attempts/{model}_openpyxl
        # Use a parallel prompts/ tree under the same agent identifier
        prompts_prefix = agent_prefix.replace("BizbenchV1/attempts/", "BizbenchV1/prompts/")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        uris = []

        # Upload system prompt
        if SYSTEM_PROMPT_PATH.exists():
            s3_key = f"{prompts_prefix}/{timestamp}_{SYSTEM_PROMPT_PATH.name}"
            try:
                self.s3_client.upload_file(str(SYSTEM_PROMPT_PATH), self._s3_bucket, s3_key)
                uri = f"s3://{self._s3_bucket}/{s3_key}"
                uris.append(uri)
                print(f"  📤 Uploaded system prompt: {uri}")
            except Exception as e:
                print(f"  ⚠️  Failed to upload system prompt: {e}")

        # Upload task templates (both variants)
        for template_path in [TASK_TEMPLATE_FMWC_PATH, TASK_TEMPLATE_WSP_PATH]:
            if template_path.exists():
                s3_key = f"{prompts_prefix}/{timestamp}_{template_path.name}"
                try:
                    self.s3_client.upload_file(str(template_path), self._s3_bucket, s3_key)
                    uri = f"s3://{self._s3_bucket}/{s3_key}"
                    uris.append(uri)
                    print(f"  📤 Uploaded template: {uri}")
                except Exception as e:
                    print(f"  ⚠️  Failed to upload template {template_path.name}: {e}")

        self._prompt_s3_uris = uris
        return uris

    def upload_result(self, task_info: TaskInfo, workspace_path: str, workspace_result: WorkspaceResult):
        """Upload result files to S3 and create TaskAttempt in DB.

        Path: BizbenchV1/attempts/{model}_openpyxl/task_source={src}/task_id={id}/{ts}_file
        """
        agent_folder = self.config['agent_folder']
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # New Hive-style path: BizbenchV1/attempts/{model}_openpyxl/task_source=X/task_id=Y
        s3_base = f"{self._s3_task_prefix(task_info)}/{timestamp}"

        workspace = Path(workspace_path)
        attempt_files = []

        # Files to upload
        files_to_upload = []

        # solution.xlsx
        solution_path = workspace / "solution.xlsx"
        if solution_path.exists():
            files_to_upload.append((solution_path, f"{s3_base}_solution.xlsx"))

        # Find agent_logs directory and its contents
        agent_logs = workspace / "agent_logs"
        if agent_logs.exists():
            # openai_requests.csv (always at agent_logs level)
            csv_path = agent_logs / "openai_requests.csv"
            if csv_path.exists():
                files_to_upload.append((csv_path, f"{s3_base}_openai_requests.csv"))

            # task.json and transcript.md (inside task_id subdirectory)
            for task_dir in sorted(agent_logs.iterdir()):
                if task_dir.is_dir() and task_dir.name.startswith("task_"):
                    task_json = task_dir / "task.json"
                    if task_json.exists():
                        files_to_upload.append((task_json, f"{s3_base}_task.json"))

                    transcript = task_dir / "transcript.md"
                    if transcript.exists():
                        files_to_upload.append((transcript, f"{s3_base}_transcript.md"))

        # Upload files to S3
        for local_path, s3_key in files_to_upload:
            try:
                self.s3_client.upload_file(str(local_path), self._s3_bucket, s3_key)
                s3_uri = f"s3://{self._s3_bucket}/{s3_key}"
                attempt_files.append(s3_uri)
                print(f"  📤 {local_path.name} -> {s3_uri}")
            except Exception as e:
                print(f"  ❌ Failed to upload {local_path.name}: {e}")

        # Times - direct from execution
        start_time_dt = datetime.fromtimestamp(workspace_result.start_time) if workspace_result.start_time else datetime.now()
        end_time_dt = datetime.fromtimestamp(workspace_result.end_time) if workspace_result.end_time else datetime.now()
        time_taken_min = workspace_result.duration_seconds / 60.0 if workspace_result.duration_seconds > 0 else 0.0

        # Cost - direct from execution (use 0 if not available, never None since column is nullable)
        total_cost = workspace_result.cost_usd if workspace_result.cost_usd > 0 else 0.0

        # Determine failure status. Hitting the iteration cap is NOT a
        # failure: the workbook was still built and uploaded, and it gets
        # judged as-is. This matches the prior benchmark wave's rows
        # (agent_failed=false with reason "Max iterations (N) reached" —
        # 215 such rows at prompt_version 1105).
        agent_failed = workspace_result.status != "success"
        agent_failed_reason = workspace_result.error_message if agent_failed else None
        if agent_failed and (agent_failed_reason or "").startswith("Max iterations"):
            agent_failed = False

        # Compute prompt_version from versioned file paths
        template_path = TASK_TEMPLATE_WSP_PATH if task_info.task_source == "wsp" else TASK_TEMPLATE_FMWC_PATH
        prompt_version = parse_prompt_version(SYSTEM_PROMPT_PATH, template_path)

        # Create TaskAttempt in DB
        prompt_files = self._prompt_s3_uris or []

        attempt = TaskAttempt(
            task_id=task_info.task_id,
            prompt_files=prompt_files,
            start_time=start_time_dt,
            end_time=end_time_dt,
            agent_model_name=agent_folder,
            agent_model_type="api",
            attempt_files=attempt_files,
            time_taken_min=time_taken_min,
            cost=total_cost,
            agent_failed=agent_failed,
            agent_failed_reason=agent_failed_reason,
            deprecated=False,
            prompt_version=prompt_version,
        )

        db = SessionLocal()
        try:
            db.add(attempt)
            db.commit()
            db.refresh(attempt)
            print(f"  ✅ Created TaskAttempt (ID: {attempt.id}, cost: ${total_cost:.4f})")
        except Exception as e:
            db.rollback()
            print(f"  ❌ Failed to create TaskAttempt: {e}")
            traceback.print_exc()
        finally:
            db.close()

    def run_batch(self) -> BatchResult:
        """Execute automated batch: resolve tasks -> setup -> execute -> upload."""
        batch_start_time = time.time()

        # Load configuration
        config = self.load_config()

        # Setup batch logging directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.batch_logs_dir = Path("batch_logs") / f"batch_{timestamp}"
        self.batch_logs_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n📂 Batch logs directory: {self.batch_logs_dir}")

        # Upload prompt snapshots once
        print(f"\n📤 Uploading prompt snapshots...")
        self.upload_prompts()

        # Resolve tasks from DB
        task_infos = self.resolve_tasks()
        if not task_infos:
            print("⚠️  No tasks to process. Exiting.")
            return BatchResult(
                batch_name=config['batch_name'],
                total_workspaces=0,
                successful=0,
                failed=0,
                workspace_results=[],
                total_duration_seconds=time.time() - batch_start_time,
                aggregated_tokens=0,
                aggregated_iterations=0,
                aggregated_cost_usd=0.0,
            )

        # Process each task
        workspace_results = []
        skipped_count = 0

        for idx, task_info in enumerate(task_infos):
            print(f"\n{'='*80}")
            print(f"📦 Task {idx + 1}/{len(task_infos)}: {task_info.task_name}")
            print(f"{'='*80}")

            # Check trial count
            if self.should_skip(task_info):
                skipped_count += 1
                continue

            workspace_path = None
            try:
                # Setup workspace (download from S3)
                workspace_path = self.setup_workspace(task_info)

                # Select appropriate task template
                task_template = self.select_task_template(task_info.task_source)
                self.config['task_template'] = task_template

                # Detect workspace files
                ws_config = self.detect_workspace_files(workspace_path)

                # Process workspace (inherited from BatchRunner)
                result = self.process_workspace(ws_config)
                workspace_results.append(result)

                # Upload result to S3 + create DB entry
                print(f"\n  📤 Uploading results...")
                self.upload_result(task_info, workspace_path, result)

                # Cleanup workspace (everything uploaded to S3)
                self.cleanup_workspace(workspace_path)

            except Exception as e:
                print(f"  💥 Error processing task '{task_info.task_name}': {e}")
                traceback.print_exc()
                workspace_results.append(WorkspaceResult(
                    workspace_path=workspace_path or "unknown",
                    status="error",
                    pdf_files=[],
                    excel_files=[],
                    task_id=None,
                    iterations=0,
                    total_tokens=0,
                    cost_usd=0.0,
                    error_message=str(e),
                    duration_seconds=0,
                    final_result=None,
                ))

        # Calculate aggregated metrics
        total_duration = time.time() - batch_start_time
        successful = sum(1 for r in workspace_results if r.status == "success")
        failed = len(workspace_results) - successful
        total_tokens = sum(r.total_tokens for r in workspace_results)
        total_iterations = sum(r.iterations for r in workspace_results)
        total_cost = sum(r.cost_usd for r in workspace_results)

        batch_result = BatchResult(
            batch_name=config['batch_name'],
            total_workspaces=len(workspace_results),
            successful=successful,
            failed=failed,
            workspace_results=workspace_results,
            total_duration_seconds=total_duration,
            aggregated_tokens=total_tokens,
            aggregated_iterations=total_iterations,
            aggregated_cost_usd=total_cost,
        )

        # Generate reports (inherited)
        self.generate_reports(batch_result)

        # Print skip info
        if skipped_count > 0:
            print(f"⏭️  Skipped (trials exhausted): {skipped_count}")

        return batch_result


def run_auto_batch_from_config(config_path: str, server_path: str, api_key: str,
                                custom_reasoning: bool = False, enable_langfuse: bool = False) -> BatchResult:
    """Entry point for auto batch execution."""
    runner = AutoBatchRunner(config_path, server_path, api_key, custom_reasoning, enable_langfuse)
    return runner.run_batch()
