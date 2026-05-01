"""LocalAttemptSink — append AttemptResult rows to an ndjson file."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..base import AttemptResult


def _json_default(o):
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


class LocalAttemptSink:
    def __init__(self, output_dir: str | Path, log_filename: str = "attempts.ndjson"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.output_dir / log_filename

    def publish(self, result: AttemptResult) -> None:
        row = asdict(result)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(row, default=_json_default) + "\n")

    def close(self) -> None:
        return None
