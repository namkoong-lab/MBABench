"""Source / sink abstractions for gui-agents.

The engine pipeline is unchanged. These two protocols are the only seam a
swappable backend needs to implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable


@dataclass
class TaskSpec:
    """What the engine needs to run one task. Source-agnostic."""

    task_id: str
    task_name: str
    upload_files: list[Path]
    solution_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttemptResult:
    """What the engine produces. The sink decides how to persist it."""

    task_id: str
    task_name: str
    agent_model_name: str
    prompt_version: int | str | None
    status: str  # "success" | "failed" | "timeout"
    solution_file: Path | None
    log_file: Path | None
    started_at: str  # ISO-8601
    finished_at: str
    duration_seconds: float
    prompt_files: list[Path] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TaskSource(Protocol):
    def iter_tasks(self) -> Iterator[TaskSpec]: ...
    def close(self) -> None: ...


@runtime_checkable
class AttemptSink(Protocol):
    def publish(self, result: AttemptResult) -> None: ...
    def close(self) -> None: ...
