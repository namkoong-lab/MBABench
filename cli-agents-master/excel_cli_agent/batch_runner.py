#!/usr/bin/env python3
"""
Batch Runner for Excel CLI Agent (Synchronous Implementation)
Processes workspaces sequentially based on YAML configuration.
"""
import os
import yaml
import time
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

from .mcp_client import ExcelMCPClient
from .task_executor import ExcelTaskExecutor, TaskStatus
from .models_config import DEFAULT_MAX_COMPLETION_TOKENS


@dataclass
class WorkspaceConfig:
    """Configuration for a single workspace"""
    path: str
    detected_pdf_files: List[str] = field(default_factory=list)
    detected_excel_files: List[str] = field(default_factory=list)


@dataclass
class WorkspaceResult:
    """Result of processing a single workspace"""
    workspace_path: str
    status: str  # "success", "failed", "error"
    pdf_files: List[str]
    excel_files: List[str]
    task_id: Optional[str]
    iterations: int
    total_tokens: int
    cost_usd: float
    error_message: Optional[str]
    duration_seconds: float
    final_result: Optional[str]
    start_time: Optional[float] = None   # epoch timestamp
    end_time: Optional[float] = None     # epoch timestamp


@dataclass
class BatchResult:
    """Aggregated results from batch processing"""
    batch_name: str
    total_workspaces: int
    successful: int
    failed: int
    workspace_results: List[WorkspaceResult]
    total_duration_seconds: float
    aggregated_tokens: int
    aggregated_iterations: int
    aggregated_cost_usd: float


class BatchRunner:
    """Batch execution engine for Excel CLI Agent (synchronous)"""

    def __init__(self, config_path: str, server_path: str, api_key: str, custom_reasoning: bool = False, enable_langfuse: bool = False):
        self.config_path = Path(config_path)
        self.server_path = server_path
        self.api_key = api_key
        self.custom_reasoning = custom_reasoning
        self.enable_langfuse = enable_langfuse
        self.config: Optional[Dict[str, Any]] = None
        self.batch_logs_dir: Optional[Path] = None

    def load_config(self) -> Dict[str, Any]:
        """Load and validate YAML configuration"""
        print(f"📋 Loading batch configuration from {self.config_path}")

        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Validate required fields
        required_fields = ['batch_name', 'model', 'task_template', 'workspaces']
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Missing required field in config: {field}")

        # Set defaults
        config.setdefault('verbose', False)
        config.setdefault('max_iterations', 30)
        config.setdefault('batch_size', 1)
        config.setdefault('snapshot_iterations', False)

        self.config = config
        print(f"✅ Configuration loaded: {config['batch_name']}")
        print(f"   Model: {config['model']}")
        print(f"   Max iterations: {config['max_iterations']}")
        print(f"   Batch size: {config['batch_size']} (sequential processing)")
        print(f"   Workspaces: {len(config['workspaces'])}")

        return config

    def detect_workspace_files(self, workspace_path: str) -> WorkspaceConfig:
        """Auto-detect source .xlsx file, all .xlsx files for context, and all .pdf files in workspace"""
        workspace = Path(workspace_path)

        if not workspace.exists():
            raise ValueError(f"Workspace does not exist: {workspace_path}")

        # Find all .xlsx files (excluding solution.xlsx) for Excel context
        xlsx_files = [f for f in workspace.glob("*.xlsx")
                      if f.name.lower() != "solution.xlsx"]
        excel_context_files = [str(f.name) for f in xlsx_files]

        # Find all .pdf files
        pdf_files = [str(f) for f in workspace.glob("*.pdf")]

        config = WorkspaceConfig(
            path=workspace_path,
            detected_pdf_files=pdf_files,
            detected_excel_files=excel_context_files
        )

        return config

    def estimate_context_tokens(self, workspace_path: str, pdf_files: List[str], excel_files: List[str]) -> int:
        """Estimate total tokens from PDF and Excel context files"""
        workspace = Path(workspace_path)
        total_estimated_tokens = 0

        # Estimate PDF tokens
        for pdf_file in pdf_files:
            pdf_path = workspace / pdf_file
            if pdf_path.exists():
                file_size = pdf_path.stat().st_size
                estimated_tokens = file_size // 4
                total_estimated_tokens += estimated_tokens

        # Estimate Excel tokens
        for excel_file in excel_files:
            excel_path = workspace / excel_file
            if excel_path.exists():
                file_size = excel_path.stat().st_size
                estimated_tokens = file_size // 6
                total_estimated_tokens += estimated_tokens

        return total_estimated_tokens

    def process_workspace(self, workspace_config: WorkspaceConfig) -> WorkspaceResult:
        """Process a single workspace with the Excel agent (synchronous)"""
        workspace_path = workspace_config.path
        start_time = time.time()

        print(f"\n{'='*80}")
        print(f"🚀 Processing workspace: {workspace_path}")
        print(f"{'='*80}")

        # Initialize result
        result = WorkspaceResult(
            workspace_path=workspace_path,
            status="error",
            pdf_files=workspace_config.detected_pdf_files,
            excel_files=workspace_config.detected_excel_files,
            task_id=None,
            iterations=0,
            total_tokens=0,
            cost_usd=0.0,
            error_message=None,
            duration_seconds=0,
            final_result=None
        )

        try:
            # Log detected files
            print(f"📄 PDF files: {len(workspace_config.detected_pdf_files)}")
            for pdf in workspace_config.detected_pdf_files:
                print(f"   - {Path(pdf).name}")
            print(f"📊 Excel files for context: {len(workspace_config.detected_excel_files)}")
            for excel in workspace_config.detected_excel_files:
                print(f"   - {excel}")

            # Estimate context size for logging/observability
            estimated_tokens = self.estimate_context_tokens(
                workspace_path,
                workspace_config.detected_pdf_files,
                workspace_config.detected_excel_files
            )
            print(f"📏 Estimated context tokens: {estimated_tokens:,}")

            # Get task description directly from template
            task_description = self.config['task_template']

            print(f"📝 Task: {task_description[:100]}...")

            # Initialize Excel client and task executor
            excel_client = ExcelMCPClient(self.server_path, workspace_path)
            langfuse_enabled = self.config.get('enable_langfuse', self.enable_langfuse)
            task_executor = ExcelTaskExecutor(
                excel_client,
                self.api_key,
                model=self.config['model'],
                custom_reasoning=self.custom_reasoning,
                fresh_context_mode=self.config.get('fresh_context_mode', False),
                enhanced_excel_context=self.config.get('enhanced_excel_context', True),
                recent_history_count=self.config.get('recent_history_count', 5),
                max_completion_tokens=self.config.get('max_completion_tokens', DEFAULT_MAX_COMPLETION_TOKENS),
                reasoning_effort=self.config.get('reasoning_effort', None),
                api_timeout_seconds=self.config.get('api_timeout_seconds', None),
                # Unified base_url (auto-detects OpenAI vs Anthropic)
                base_url=self.config.get('base_url', None),
                # Legacy flags (still supported for backward compat)
                use_anthropic_direct=self.config.get('use_anthropic_direct', False),
                anthropic_api_key=self.config.get('anthropic_api_key', None),
                thinking_budget_tokens=self.config.get('thinking_budget_tokens', None),
                anthropic_effort=self.config.get('anthropic_effort', None),
                use_openai_direct=self.config.get('use_openai_direct', False),
                # Versioned system prompt path
                system_prompt_path=self.config.get('system_prompt_path', None),
            )

            # Configure executor
            task_executor.set_max_iterations(self.config['max_iterations'])
            task_executor.set_verbose(self.config['verbose'])
            task_executor.snapshot_iterations = self.config['snapshot_iterations']

            # Connect to Excel MCP server (synchronous)
            excel_client.connect()

            # Add PDF context
            if workspace_config.detected_pdf_files:
                add_result = task_executor.add_context_pdfs(workspace_config.detected_pdf_files)
                print(f"✅ Added {len(add_result['added'])} PDFs to context")

            # Add Excel context
            if workspace_config.detected_excel_files:
                add_result = task_executor.add_context_excels(workspace_config.detected_excel_files)
                print(f"✅ Added {len(add_result['added'])} Excel file(s) to context")

            # Show configuration
            print(f"🧠 Model: {self.config['model']}")
            langfuse_status = "enabled" if task_executor.langfuse_enabled else "disabled"
            print(f"🪢 Langfuse: {langfuse_status}")
            print(f"⚙️  Max iterations: {self.config['max_iterations']}")

            # Execute task (synchronous)
            task_execution = task_executor.execute_task(task_description)

            # Disconnect client (synchronous)
            excel_client.disconnect()

            # Update result
            result.task_id = task_execution.task_id
            result.iterations = task_execution.total_iterations
            result.final_result = task_execution.final_result

            if task_execution.status == TaskStatus.COMPLETED:
                result.status = "success"
                # Preserve info messages (e.g., "Max iterations reached") even on success
                if task_execution.error:
                    result.error_message = task_execution.error
                    print(f"✅ Workspace completed ({task_execution.error})")
                else:
                    print(f"✅ Workspace completed successfully")
            else:
                result.status = "failed"
                result.error_message = task_execution.error or f"Task status: {task_execution.status.value}"
                print(f"❌ Workspace failed: {result.error_message}")

            # Populate cost and timing directly from TaskExecution
            result.cost_usd = task_execution.total_cost_usd
            result.start_time = task_execution.start_time
            result.end_time = task_execution.end_time

        except Exception as e:
            result.status = "error"
            result.error_message = str(e)
            print(f"💥 Error processing workspace: {str(e)}")

        finally:
            result.duration_seconds = time.time() - start_time
            print(f"⏱️  Duration: {result.duration_seconds:.2f}s")

        return result

    def run_batch(self) -> BatchResult:
        """Execute batch processing of all workspaces (synchronous, sequential)"""
        batch_start_time = time.time()

        # Load configuration
        config = self.load_config()

        # Setup batch logging directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.batch_logs_dir = Path("batch_logs") / f"batch_{timestamp}"
        self.batch_logs_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n📂 Batch logs directory: {self.batch_logs_dir}")

        # Detect files in all workspaces
        print(f"\n🔍 Detecting files in workspaces...")
        workspace_configs = []
        for ws_entry in config['workspaces']:
            ws_path = os.path.expanduser(ws_entry['path'])
            try:
                ws_config = self.detect_workspace_files(ws_path)
                workspace_configs.append(ws_config)
                print(f"✅ {ws_path}: {len(ws_config.detected_pdf_files)} PDFs, {len(ws_config.detected_excel_files)} Excel files for context")
            except Exception as e:
                print(f"❌ {ws_path}: Error - {str(e)}")

        print(f"\n🚀 Starting batch processing (sequential, {len(workspace_configs)} workspaces)")

        # Process workspaces sequentially (simple for loop)
        workspace_results = []
        for idx, ws_config in enumerate(workspace_configs):
            print(f"\n📦 Workspace {idx + 1}/{len(workspace_configs)}")
            result = self.process_workspace(ws_config)
            workspace_results.append(result)

        # Calculate aggregated metrics
        total_duration = time.time() - batch_start_time
        successful = sum(1 for r in workspace_results if r.status == "success")
        failed = len(workspace_results) - successful
        total_tokens = sum(r.total_tokens for r in workspace_results)
        total_iterations = sum(r.iterations for r in workspace_results)
        total_cost = sum(r.cost_usd for r in workspace_results)

        # Create batch result
        batch_result = BatchResult(
            batch_name=config['batch_name'],
            total_workspaces=len(workspace_results),
            successful=successful,
            failed=failed,
            workspace_results=workspace_results,
            total_duration_seconds=total_duration,
            aggregated_tokens=total_tokens,
            aggregated_iterations=total_iterations,
            aggregated_cost_usd=total_cost
        )

        # Generate reports
        self.generate_reports(batch_result)

        return batch_result

    def generate_reports(self, batch_result: BatchResult):
        """Generate summary reports for batch execution"""

        # Create summary markdown
        summary_lines = [
            f"# Batch Execution Summary: {batch_result.batch_name}",
            "",
            f"**Execution Date**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Total Duration**: {batch_result.total_duration_seconds:.2f} seconds",
            "",
            "## Overview",
            "",
            f"- **Total Workspaces**: {batch_result.total_workspaces}",
            f"- **Successful**: {batch_result.successful}",
            f"- **Failed**: {batch_result.failed}",
            f"- **Total Iterations**: {batch_result.aggregated_iterations}",
            f"- **Total Tokens**: {batch_result.aggregated_tokens:,}",
            f"- **Total Cost**: ${batch_result.aggregated_cost_usd:.4f}",
            "",
            "## Workspace Results",
            ""
        ]

        for result in batch_result.workspace_results:
            status_emoji = "✅" if result.status == "success" else "❌"
            summary_lines.append(f"### {status_emoji} {result.workspace_path}")
            summary_lines.append("")
            summary_lines.append(f"- **Status**: {result.status}")
            summary_lines.append(f"- **PDF Files**: {len(result.pdf_files)}")
            summary_lines.append(f"- **Excel Context Files**: {len(result.excel_files)}")
            summary_lines.append(f"- **Iterations**: {result.iterations}")
            summary_lines.append(f"- **Tokens**: {result.total_tokens:,}")
            summary_lines.append(f"- **Cost**: ${result.cost_usd:.4f}")
            summary_lines.append(f"- **Duration**: {result.duration_seconds:.2f}s")

            if result.final_result:
                summary_lines.append(f"- **Result**: {result.final_result}")

            if result.error_message:
                summary_lines.append(f"- **Error**: {result.error_message}")

            summary_lines.append("")

        summary_path = self.batch_logs_dir / "summary.md"
        summary_path.write_text("\n".join(summary_lines))

        # Create aggregated metrics JSON
        metrics = {
            "batch_name": batch_result.batch_name,
            "execution_timestamp": datetime.now().isoformat(),
            "total_workspaces": batch_result.total_workspaces,
            "successful": batch_result.successful,
            "failed": batch_result.failed,
            "total_duration_seconds": batch_result.total_duration_seconds,
            "aggregated_tokens": batch_result.aggregated_tokens,
            "aggregated_iterations": batch_result.aggregated_iterations,
            "aggregated_cost_usd": round(batch_result.aggregated_cost_usd, 4),
            "_note": "Full task details with database schema fields available in each workspace's agent_logs/<task_id>/task.json",
            "workspace_results": [
                {
                    "workspace_path": r.workspace_path,
                    "status": r.status,
                    "task_id": r.task_id,
                    "task_json_path": f"{r.workspace_path}/agent_logs/{r.task_id}/task.json" if r.task_id else None,
                    "pdf_count": len(r.pdf_files),
                    "excel_context_count": len(r.excel_files),
                    "iterations": r.iterations,
                    "tokens": r.total_tokens,
                    "cost_usd": round(r.cost_usd, 4),
                    "duration_seconds": r.duration_seconds,
                    "error": r.error_message
                }
                for r in batch_result.workspace_results
            ]
        }

        metrics_path = self.batch_logs_dir / "aggregated_metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2))

        print(f"\n{'='*80}")
        print(f"📊 BATCH EXECUTION COMPLETE")
        print(f"{'='*80}")
        print(f"✅ Successful: {batch_result.successful}/{batch_result.total_workspaces}")
        print(f"❌ Failed: {batch_result.failed}/{batch_result.total_workspaces}")
        print(f"📈 Total iterations: {batch_result.aggregated_iterations}")
        print(f"🔢 Total tokens: {batch_result.aggregated_tokens:,}")
        print(f"💰 Total cost: ${batch_result.aggregated_cost_usd:.4f}")
        print(f"⏱️  Total duration: {batch_result.total_duration_seconds:.2f}s")
        print(f"📂 Reports saved to: {self.batch_logs_dir}")
        print(f"{'='*80}")


def run_batch_from_config(config_path: str, server_path: str, api_key: str, custom_reasoning: bool = False, enable_langfuse: bool = False) -> BatchResult:
    """Entry point for batch execution (synchronous)"""
    runner = BatchRunner(config_path, server_path, api_key, custom_reasoning, enable_langfuse)
    return runner.run_batch()
