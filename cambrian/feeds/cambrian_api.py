"""Client for the Cambrian API (Base chain: DEX pools + lending).

Confirmed by probing the live API:
  * Base URL:  https://api.cambrian.org   (override via CAMBRIAN_BASE_URL)
  * Auth:      header  x-api-key: <key>   (NOT Authorization: Bearer)
  * Responses are COLUMNAR, ClickHouse-style:
        [{"columns":[{"name":"id","type":"UInt32"}, ...],
          "data":[[8453,"base"], ...], "rows":N}]
    so a row is a list aligned to `columns`. `rows_from_response` flattens that
    into a list of dicts.
  * Coverage today: chain `base` (8453) only; Uniswap v3 (no v4); plus Aerodrome,
    Pancake, Sushi, AlienBase, Clones, and Aave/Euler/Morpho lending.

The key is read from CAMBRIAN_API_KEY (put it in .env) — never hardcode it.
"""

from __future__ import annotations

import os
from typing import Any

import requests

BASE_URL = os.getenv("CAMBRIAN_BASE_URL", "https://api.cambrian.org")


class CambrianError(RuntimeError):
    pass


def rows_from_response(payload: Any) -> list[dict[str, Any]]:
    """Flatten Cambrian's columnar response into a list of dict rows.

    Tolerant: accepts the list-wrapped columnar form, a bare columnar dict, a
    plain list of dicts, or a {"data": [...]} envelope.
    """
    if (isinstance(payload, list) and payload and isinstance(payload[0], dict)
            and "columns" in payload[0] and "data" in payload[0]):
        payload = payload[0]
    if isinstance(payload, dict) and "columns" in payload and "data" in payload:
        cols = [c["name"] if isinstance(c, dict) else c for c in payload["columns"]]
        return [dict(zip(cols, row)) for row in payload["data"]]
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [r for r in payload["data"] if isinstance(r, dict)]
    return []


def paginate(fetch, *, page_size: int, max_pages: int, key: str | None):
    """Accumulate rows across pages until exhausted, deduping by `key`.

    Robust to unknown pagination: `fetch(offset)` is called with rising offsets;
    if the API ignores the offset (returns the same page), the dedupe sees zero
    new rows and stops — so a wrong offset param degrades to a single page rather
    than looping forever or double-counting.
    """
    seen: set = set()
    out: list = []
    for i in range(max_pages):
        rows = fetch(i * page_size)
        if not rows:
            break
        new = 0
        for r in rows:
            k = r.get(key) if (key and isinstance(r, dict)) else id(r)
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
            new += 1
        if new == 0 or len(rows) < page_size:
            break
    return out


class CambrianClient:
    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL,
                 timeout: float = 15.0):
        self.api_key = api_key or os.getenv("CAMBRIAN_API_KEY", "")
        if not self.api_key:
            raise CambrianError("CAMBRIAN_API_KEY is not set (put it in .env)")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, **params) -> Any:
        """Raw GET — returns the parsed JSON payload as-is (for --raw / probing)."""
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        resp = requests.get(self.base_url + path, headers=headers,
                            params={k: v for k, v in params.items() if v is not None},
                            timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise CambrianError(f"{resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()

    def query(self, path: str, **params) -> list[dict[str, Any]]:
        """GET and flatten the columnar response into row dicts."""
        return rows_from_response(self.get(path, **params))

    def query_all(self, path: str, *, page_size: int = 1000, max_pages: int = 50,
                  offset_param: str = "offset", key: str = "poolId",
                  **params) -> list[dict[str, Any]]:
        """Page through an endpoint to get the ENTIRE set, not just the first page.

        Dedupes by `key` (pool id), so if the offset param is wrong the loop stops
        after one page instead of duplicating — see `paginate`.
        """
        def fetch(offset: int) -> list[dict[str, Any]]:
            return self.query(path, **{**params, "limit": page_size,
                                       offset_param: offset})
        return paginate(fetch, page_size=page_size, max_pages=max_pages, key=key)

    # --- Convenience --------------------------------------------------------

    def chains(self) -> list[dict[str, Any]]:
        return self.query("/evm/chains")

    def pools(self, pools_endpoint: str, **params) -> list[dict[str, Any]]:
        return self.query(pools_endpoint, **params)

    def lending_overview(self, **params) -> list[dict[str, Any]]:
        return self.query("/evm/lending/overview", **params)
