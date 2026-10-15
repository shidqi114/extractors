from __future__ import annotations

import json
import os
from pathlib import Path


class State:
    """Tiny JSON progress file so an interrupted dump can resume at table level."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"dump": {}}
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            self.data.setdefault("dump", {})

    def dump_done(self, table: str) -> bool:
        return table in self.data["dump"]

    def dump_rows(self, table: str) -> int:
        return int(self.data["dump"].get(table, 0))

    def mark_dump_done(self, table: str, rows: int) -> None:
        self.data["dump"][table] = rows
        self._save()

    def reset_dump(self) -> None:
        self.data["dump"] = {}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
