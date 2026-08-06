"""Standalone Base LP yield scanner — no package, no repo, just this file.

Save it anywhere, set your key, run it:

    # PowerShell
    $env:CAMBRIAN_API_KEY = "your-key"
    python scan_base_standalone.py

Ranks Base DEX pools by all-in APR and shows how much of that is real swap fees
vs. token emissions. Read-only — it only reads Cambrian, moves no funds.
"""

import os
import sys

import requests

API_KEY = os.getenv("CAMBRIAN_API_KEY", "")
BASE = os.getenv("CAMBRIAN_BASE_URL", "https://api.cambrian.org")
MIN_TVL = 250_000  # skip pools thinner than this

ENDPOINTS = {
    "aerodrome-v2": "/evm/aero/v2/pools",
    "uniswap-v3": "/evm/uniswap/v3/pools",
    "pancake-v3": "/evm/pancake/v3/pools",
    "sushi-v3": "/evm/sushi/v3/pools",
    "alienbase-v3": "/evm/alien/v3/pools",
    "clones-v3": "/evm/clones/v3/pools",
}


def rows(payload):
    """Flatten Cambrian's columnar {columns,data,rows} response into dicts."""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) \
            and "columns" in payload[0]:
        payload = payload[0]
    if isinstance(payload, dict) and "columns" in payload:
        cols = [c["name"] for c in payload["columns"]]
        return [dict(zip(cols, r)) for r in payload["data"]]
    return payload if isinstance(payload, list) else []


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get(path):
    r = requests.get(BASE + path, headers={"x-api-key": API_KEY}, timeout=20)
    r.raise_for_status()
    return rows(r.json())


def normalize(row, dex):
    tvl = num(row.get("poolTvlUsd") or row.get("tvl_usd"))
    swap = num(row.get("swapFeeApr7d") or row.get("fee_apr"))
    reward = num(row.get("aeroRewardApr7d"))
    total = num(row.get("totalApr7d"))
    if total is None:
        parts = [x for x in (swap, reward, num(row.get("bribeFeeApr7d")))
                 if x is not None]
        total = sum(parts) if parts else swap
    s0, s1 = row.get("token0Symbol"), row.get("token1Symbol")
    return {"dex": dex, "pair": f"{s0}/{s1}" if s0 and s1 else "?",
            "tvl": tvl, "apr": total, "fees": swap}


def main():
    if not API_KEY:
        print("Set your key first:  $env:CAMBRIAN_API_KEY = \"your-key\"")
        return 1

    pools = []
    for dex, endpoint in ENDPOINTS.items():
        try:
            pools += [normalize(r, dex) for r in get(endpoint)]
        except Exception as exc:
            print(f"  skip {dex}: {exc}")

    ranked = sorted(
        [p for p in pools if p["tvl"] and p["tvl"] >= MIN_TVL and p["apr"] is not None],
        key=lambda p: p["apr"], reverse=True,
    )[:25]

    print(f"\n{len(pools)} pools scanned, {len(ranked)} above ${MIN_TVL:,.0f} TVL\n")
    print(f"{'DEX':<14}{'PAIR':<16}{'TVL':>14}{'ALL-IN APR':>12}{'FEES ONLY':>11}")
    for p in ranked:
        print(f"{p['dex']:<14}{p['pair'][:16]:<16}{p['tvl']:>14,.0f}"
              f"{p['apr']:>11.2%}{(p['fees'] or 0):>11.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
