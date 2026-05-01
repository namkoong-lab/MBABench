"""YamlTaskSource — reads per-task YAMLs.

`yaml_path` may be either:
  * a directory — every *.yaml file in it is loaded; each file is either one
    task (top-level reserved fields) or a `tasks:` list.
  * a single file — same two shapes are accepted.

Reserved task fields (the only ones this source cares about):
    task_name, upload_files, files_to_upload, solution_name, skip, task_source

Any non-reserved top-level keys are IGNORED here. The runner applies
project-wide overrides at the --run-config layer; there is no per-task
override layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import yaml

from ..base import TaskSpec


class YamlTaskSource:
    def __init__(
        self,
        yaml_path: str | Path,
        local_files_base: str | Path | None = None,
    ):
        self.yaml_path = Path(yaml_path)
        self.local_files_base = Path(local_files_base) if local_files_base else None
        if not self.yaml_path.exists():
            raise FileNotFoundError(f"Tasks YAML path not found: {self.yaml_path}")

    def _iter_source_files(self) -> Iterator[Path]:
        if self.yaml_path.is_dir():
            yield from sorted(self.yaml_path.glob("*.yaml"))
            yield from sorted(self.yaml_path.glob("*.yml"))
        else:
            yield self.yaml_path

    def iter_tasks(self) -> Iterator[TaskSpec]:
        task_index = 0
        for file_path in self._iter_source_files():
            with open(file_path) as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                continue

            file_default_source = data.get("task_source")

            if isinstance(data.get("tasks"), list):
                # Multi-task file
                for task in data["tasks"]:
                    spec = self._build_spec(
                        task, task_index, file_default_source, file_path
                    )
                    if spec is not None:
                        task_index += 1
                        yield spec
            else:
                # Single-task file — the YAML itself IS the task
                if data.get("skip"):
                    continue
                spec = self._build_spec(
                    data, task_index, file_default_source, file_path
                )
                if spec is not None:
                    task_index += 1
                    yield spec

    def _build_spec(
        self,
        task,
        idx: int,
        file_default_source: str | None,
        file_path: Path,
    ) -> TaskSpec | None:
        if isinstance(task, str):
            task_name = task
            upload_files_raw: list = []
            solution_name = None
            task_source = file_default_source or "yaml"
        elif isinstance(task, dict):
            if task.get("skip"):
                return None
            task_name = task.get("task_name", f"task_{idx}")
            upload_files_raw = (
                task.get("upload_files") or task.get("files_to_upload") or []
            )
            solution_name = task.get("solution_name")
            task_source = task.get("task_source", file_default_source or "yaml")
        else:
            return None

        resolved: list[Path] = []
        for f in upload_files_raw:
            p = Path(f)
            if not p.is_absolute() and self.local_files_base is not None:
                p = (self.local_files_base / p).resolve()
            resolved.append(p)

        return TaskSpec(
            task_id=str(task_name),
            task_name=task_name,
            upload_files=resolved,
            solution_name=solution_name,
            metadata={
                "task_source": task_source,
                "source_file": str(file_path),
            },
        )

    def close(self) -> None:
        return None
