"""Normalize Cambrian pool rows into a common shape, then filter + rank by yield.

The one soft spot in this whole pivot: I built it without live API access, so the
exact COLUMN NAMES each pools endpoint returns are best guesses. They're all
isolated in ALIASES below. `normalize_pool` tries each alias; if a field comes
back None on real data, run `scan-base --raw` to see the true column names and
add them here — it's a one-line fix, not a rewrite. The columnar parsing itself
(rows_from_response) is confirmed against the live format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Candidate column names, most-specific first. Add real ones after `--raw`.
ALIASES = {
    "address": ("pool_address", "address", "pool", "id", "pool_id"),
    "tvl_usd": ("tvl_usd", "tvl", "liquidity_usd", "total_value_locked_usd", "reserve_usd"),
    "fee_apr": ("fee_apr", "apr", "fees_apr", "fee_apy", "apr_fees"),
    "volume_usd": ("volume_usd_24h", "volume_24h_usd", "volume_usd", "volume_24h", "daily_volume_usd"),
    "fee_rate": ("fee_rate", "fee_tier", "fee", "swap_fee"),
    "sym0": ("token0_symbol", "symbol0", "base_symbol", "token0"),
    "sym1": ("token1_symbol", "symbol1", "quote_symbol", "token1"),
    "addr0": ("token0_address", "token0", "base_address"),
    "addr1": ("token1_address", "token1", "quote_address"),
}


@dataclass(frozen=True)
class PoolYield:
    dex: str
    address: str
    label: str
    tvl_usd: float | None
    fee_apr: float | None   # fraction (0.12 == 12% APR)
    volume_usd: float | None
    token_addrs: tuple[str | None, str | None]


def _pick(row: dict[str, Any], key: str) -> Any:
    for name in ALIASES[key]:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fee_rate_fraction(v: Any) -> float | None:
    """Fee tier may be a fraction (0.003), bps (30), or a raw tier (3000 = 0.3%).
    Normalize to a fraction, conservatively."""
    n = _num(v)
    if n is None:
        return None
    if n <= 0:
        return None
    if n < 1:            # already a fraction
        return n
    if n <= 100:         # basis points
        return n / 10_000
    return n / 1_000_000  # raw uni-style tier (e.g. 3000 -> 0.003)


def normalize_pool(row: dict[str, Any], dex: str) -> PoolYield:
    tvl = _num(_pick(row, "tvl_usd"))
    vol = _num(_pick(row, "volume_usd"))
    apr = _num(_pick(row, "fee_apr"))
    # If the endpoint didn't hand us an APR, derive it from fee income vs TVL.
    if apr is None:
        rate = _fee_rate_fraction(_pick(row, "fee_rate"))
        if rate is not None and vol is not None and tvl:
            apr = (vol * rate * 365.0) / tvl
    s0, s1 = _pick(row, "sym0"), _pick(row, "sym1")
    label = f"{s0}/{s1}" if s0 and s1 else str(_pick(row, "address") or "?")
    return PoolYield(
        dex=dex,
        address=str(_pick(row, "address") or ""),
        label=label,
        tvl_usd=tvl,
        fee_apr=apr,
        volume_usd=vol,
        token_addrs=(_lower(_pick(row, "addr0")), _lower(_pick(row, "addr1"))),
    )


def _lower(v: Any) -> str | None:
    return str(v).lower() if v else None


def scan(pools: list[PoolYield], *, min_tvl_usd: float,
         max_results: int = 25) -> list[PoolYield]:
    """Keep pools that clear the TVL floor and have a known fee APR; rank by APR."""
    kept = [p for p in pools
            if p.tvl_usd is not None and p.tvl_usd >= min_tvl_usd
            and p.fee_apr is not None]
    kept.sort(key=lambda p: p.fee_apr or 0.0, reverse=True)
    return kept[:max_results]


def pools_holding(pools: list[PoolYield], token: str) -> list[PoolYield]:
    """Pools that include `token` (address, case-insensitive)."""
    t = token.lower()
    return [p for p in pools if t in (a for a in p.token_addrs if a)]
