"""Normalize Cambrian pool rows into a common shape, then filter + rank by yield.

Column names are mapped from the live Aerodrome pools payload (confirmed):
  poolId, token0/token1 (+Symbol/Decimals), poolTvlUsd, volume24hUsd,
  fees7dUsd, swapFeeApr7d, aeroRewardApr7d, bribeFeeApr7d, totalApr7d.
APRs come back as fractions (0.0424 == 4.24%).

Key nuance Aerodrome exposes: yield splits into durable swap fees vs volatile
emissions/bribes. We keep BOTH — `swap_fee_apr` (real trading fees) and
`fee_apr` (all-in, incl. incentives) — so a pool that's 90% AERO emissions
doesn't masquerade as safe fee income.

Other DEX endpoints (uniswap/pancake/sushi) may name columns differently; their
aliases are educated guesses. `scan-base --raw` prints the truth for any endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Candidate column names per field, most-specific first.
ALIASES = {
    "address": ("poolId", "pool_address", "address", "pool", "id", "pool_id"),
    "tvl_usd": ("poolTvlUsd", "tvl_usd", "tvl", "liquidity_usd", "total_value_locked_usd"),
    "swap_fee_apr": ("swapFeeApr7d", "swap_fee_apr", "feeApr", "fee_apr", "fees_apr"),
    "reward_apr": ("aeroRewardApr7d", "reward_apr", "rewardApr", "incentive_apr"),
    "bribe_apr": ("bribeFeeApr7d", "bribe_apr", "bribeApr"),
    "total_apr": ("totalApr7d", "total_apr", "totalApr", "apr", "apy"),
    "volume_usd": ("volume24hUsd", "volume_usd_24h", "volume24h", "volume_usd", "daily_volume_usd"),
    "fees_7d_usd": ("fees7dUsd", "fees_7d_usd", "fees7d"),
    "fee_rate": ("fee_rate", "fee_tier", "fee", "swap_fee"),
    "sym0": ("token0Symbol", "token0_symbol", "symbol0", "base_symbol"),
    "sym1": ("token1Symbol", "token1_symbol", "symbol1", "quote_symbol"),
    "addr0": ("token0", "token0_address", "base_address"),
    "addr1": ("token1", "token1_address", "quote_address"),
}


@dataclass(frozen=True)
class PoolYield:
    dex: str
    address: str
    label: str
    tvl_usd: float | None
    #: All-in yield the LP earns: swap fees + emissions + bribes (a fraction).
    fee_apr: float | None
    #: Durable swap-fee-only portion — excludes emissions, which can vanish.
    swap_fee_apr: float | None
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
    n = _num(v)
    if n is None or n <= 0:
        return None
    if n < 1:
        return n            # already a fraction
    if n <= 100:
        return n / 10_000   # basis points
    return n / 1_000_000    # raw uni-style tier (3000 -> 0.003)


def normalize_pool(row: dict[str, Any], dex: str) -> PoolYield:
    tvl = _num(_pick(row, "tvl_usd"))
    vol = _num(_pick(row, "volume_usd"))
    swap = _num(_pick(row, "swap_fee_apr"))

    # Fall back to deriving swap-fee APR if the column is absent.
    if swap is None:
        fees7d = _num(_pick(row, "fees_7d_usd"))
        if fees7d is not None and tvl:
            swap = fees7d / tvl * (365.0 / 7.0)
        else:
            rate = _fee_rate_fraction(_pick(row, "fee_rate"))
            if rate is not None and vol is not None and tvl:
                swap = (vol * rate * 365.0) / tvl

    reward = _num(_pick(row, "reward_apr"))
    bribe = _num(_pick(row, "bribe_apr"))
    total = _num(_pick(row, "total_apr"))
    if total is None:
        parts = [x for x in (swap, reward, bribe) if x is not None]
        total = sum(parts) if parts else None

    s0, s1 = _pick(row, "sym0"), _pick(row, "sym1")
    label = f"{s0}/{s1}" if s0 and s1 else str(_pick(row, "address") or "?")
    return PoolYield(
        dex=dex,
        address=str(_pick(row, "address") or ""),
        label=label,
        tvl_usd=tvl,
        fee_apr=total,
        swap_fee_apr=swap,
        volume_usd=vol,
        token_addrs=(_lower(_pick(row, "addr0")), _lower(_pick(row, "addr1"))),
    )


def _lower(v: Any) -> str | None:
    return str(v).lower() if v else None


def scan(pools: list[PoolYield], *, min_tvl_usd: float,
         max_results: int = 25) -> list[PoolYield]:
    """Keep pools that clear the TVL floor and have a known all-in APR; rank by it."""
    kept = [p for p in pools
            if p.tvl_usd is not None and p.tvl_usd >= min_tvl_usd
            and p.fee_apr is not None]
    kept.sort(key=lambda p: p.fee_apr or 0.0, reverse=True)
    return kept[:max_results]


def pools_holding(pools: list[PoolYield], token: str) -> list[PoolYield]:
    t = token.lower()
    return [p for p in pools if t in (a for a in p.token_addrs if a)]
