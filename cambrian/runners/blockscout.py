"""Blockscout explorer client — token holders + top-holder concentration.

RH Chain's explorer is a Blockscout instance (config.EXPLORER), whose v2 REST API
serves token metadata and the holder list. That's the rug-filter data the runner
scorer needs. The HTTP calls are thin; the parsing/computation is pure and tested.
"""

from __future__ import annotations

from typing import Any

import requests

from .. import config as chain_cfg


class Blockscout:
    def __init__(self, base_url: str | None = None, *, timeout: float = 10.0):
        self.base = (base_url or chain_cfg.EXPLORER).rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, **params) -> Any:
        r = requests.get(self.base + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def token(self, token: str) -> dict:
        return self._get(f"/api/v2/tokens/{token}")

    def holders(self, token: str) -> dict:
        return self._get(f"/api/v2/tokens/{token}/holders")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def holder_count(token_json: dict) -> int | None:
    v = token_json.get("holders") or token_json.get("holders_count")
    n = _num(v)
    return int(n) if n is not None else None


def top_holder_pct(token_json: dict, holders_json: dict) -> float | None:
    """Largest holder's share of supply, as a fraction. Raw values cancel the
    decimals, so no scaling needed."""
    supply = _num(token_json.get("total_supply"))
    if not supply:
        return None
    items = holders_json.get("items") or holders_json.get("holders") or []
    top = max((_num(i.get("value")) or 0.0 for i in items), default=0.0)
    return top / supply if supply else None
