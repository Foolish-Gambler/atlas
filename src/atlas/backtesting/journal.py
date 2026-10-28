"""JSONL audit journal for point-in-time backtests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonlJournal:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        self.records.append(record)

    def dumps(self) -> str:
        return "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in self.records)

    def persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.dumps(), encoding="utf-8")
