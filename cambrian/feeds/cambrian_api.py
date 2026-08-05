"""Client for the Cambrian financial-intelligence API — the LP desk's live data.

Cambrian indexes DEX pools (TVL, fees, yield, liquidity positions), which is
exactly what `LPPool` needs. This swaps the fixture feed for real pool data:
`pool_snapshot()` returns an `LPPool` the desk can evaluate directly.

Two things aren't hardcoded because they come from your Cambrian dashboard/docs,
not from guessing:
  * BASE_URL and the auth header format (env-overridable below).
  * The exact endpoint path and the JSON field names for TVL / fee-APR — isolated
    in ENDPOINTS and FIELDS at the top so you edit one place after reading the docs.

The key is read from CAMBRIAN_API_KEY (put it in .env) — never hardcode it.
Run `python -m cambrian probe-cambrian` to auth-check and see a raw pool payload,
then fill in FIELDS to match what you see.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from ..snapshots import LPPool

BASE_URL = os.getenv("CAMBRIAN_BASE_URL", "https://api.cambrian.org")

# --- Fill from your Cambrian docs -------------------------------------------
# Endpoint paths (relative to BASE_URL). Confirm exact paths in the dashboard.
ENDPOINTS = {
    "health": "/v1/health",            # any cheap authenticated endpoint, for probe
    "pool": "/v1/dex/pools",           # pool lookup/discovery
}
# JSON field names in a pool object -> what the desk needs. Confirm against a
# real payload (probe prints one).
FIELDS = {
    "tvl_usd": "tvl_usd",
    "fee_apr": "fee_apr",              # as a fraction (0.25 == 25% APR)
    "pool_id": "address",
}
# ---------------------------------------------------------------------------


class CambrianError(RuntimeError):
    pass


class CambrianClient:
    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL,
                 timeout: float = 10.0):
        self.api_key = api_key or os.getenv("CAMBRIAN_API_KEY", "")
        if not self.api_key:
            raise CambrianError("CAMBRIAN_API_KEY is not set (put it in .env)")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, **params) -> Any:
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Accept": "application/json"}
        resp = requests.get(self.base_url + path, headers=headers,
                            params=params, timeout=self.timeout)
        if resp.status_code == 401:
            raise CambrianError("401 Unauthorized — check the key and header format")
        resp.raise_for_status()
        return resp.json()

    def probe(self) -> Any:
        """Auth-check against the health endpoint. Returns the raw response."""
        return self._get(ENDPOINTS["health"])

    def raw_pool(self, stock_token: str, quote_token: str) -> Any:
        """Raw pool payload for a (stock, quote) pair — inspect it to fill FIELDS."""
        return self._get(ENDPOINTS["pool"], token0=stock_token, token1=quote_token)

    def pool_snapshot(self, stock_token: str, quote_token: str,
                      position_usd: float) -> LPPool:
        """Fetch and map a Cambrian pool into an LPPool the desk can evaluate."""
        data = self.raw_pool(stock_token, quote_token)
        pool = data[0] if isinstance(data, list) else data.get("data", data)
        if isinstance(pool, list):
            pool = pool[0]
        return LPPool(
            pool=str(pool.get(FIELDS["pool_id"], "")),
            stock_token=stock_token,
            quote_token=quote_token,
            tvl_usd=_num(pool.get(FIELDS["tvl_usd"])),
            fee_apr=_num(pool.get(FIELDS["fee_apr"])),
            position_usd=position_usd,
        )


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
