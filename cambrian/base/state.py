"""Persist the paper portfolio's current positions between runs.

The rebalancer needs to know what you're 'holding' to compute the delta. In
dry-run that's a simple JSON file mapping pool address -> {usd, label}. When live
execution is wired, this gets replaced (or reconciled against) real on-chain
position state.
"""

from __future__ import annotations

import json
import os
from typing import Any


def load_positions(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def current_usd(path: str) -> dict[str, float]:
    return {pool: float(rec.get("usd", 0.0))
            for pool, rec in load_positions(path).items()}


def save_positions(path: str, positions: dict[str, dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(positions, fh, indent=2, sort_keys=True)
