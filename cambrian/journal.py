"""Append-only order journal.

Every decision and every order attempt lands here as one JSON object per line,
flushed and fsync'd before we return. The journal is the system's memory and
its accountability: if it isn't in the journal, it didn't happen. Nothing in
this module ever rewrites or truncates the file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Journal:
    def __init__(self, path: str):
        self.path = path

    def record(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one event. Returns the full record (with timestamp) written.

        `kind` is a coarse tag ("decision", "order", "halt", ...). `payload` is
        whatever that event needs; it must be JSON-serializable.
        """
        record = {"ts": _utc_now_iso(), "kind": kind, **payload}
        line = json.dumps(record, default=str, sort_keys=True)
        # Open per-write in append mode so a crash can never leave a truncated
        # file, and so concurrent desk processes interleave whole lines rather
        # than corrupting each other's.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return record

    def read_all(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        return self.read_all()[-n:]

    def filter(self, **match: Any) -> Iterable[dict[str, Any]]:
        for rec in self.read_all():
            if all(rec.get(k) == v for k, v in match.items()):
                yield rec
