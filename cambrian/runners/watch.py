"""State + dedup for the continuous watch loop.

The loop rescans every interval; without memory it would re-alert the same
runners forever. `SeenStore` remembers which tokens we've already surfaced (a
JSON set of lowercased addresses), and `select_new` returns only the ones we
haven't. Pure and tested; the loop itself lives in the CLI.
"""

from __future__ import annotations

import json
import os


def load_seen(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as fh:
        return {str(t).lower() for t in json.load(fh)}


def save_seen(path: str, seen: set[str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sorted(seen), fh)


def select_new(ranked: list, seen: set[str]) -> list:
    """Runners whose token isn't already in `seen` (case-insensitive)."""
    return [s for s in ranked if s.candidate.token.lower() not in seen]
