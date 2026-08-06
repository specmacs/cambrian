"""Normalize Cambrian lending rows into per-market supply yields.

Same caveat as pools.py: exact column names are best guesses until a live
`--raw` confirms them. Isolated in ALIASES.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ALIASES = {
    "symbol": ("symbol", "asset_symbol", "token_symbol", "underlying_symbol", "asset"),
    "address": ("asset_address", "underlying_address", "token_address", "address"),
    "supply_apr": ("supply_apr", "supply_apy", "supply_rate", "lend_apr", "apy", "apr"),
    "protocol": ("protocol", "market", "source"),
    "tvl_usd": ("tvl_usd", "supply_usd", "total_supply_usd", "liquidity_usd"),
}


@dataclass(frozen=True)
class LendingMarket:
    protocol: str | None
    symbol: str | None
    address: str | None
    supply_apr: float | None
    tvl_usd: float | None


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


def normalize_market(row: dict[str, Any]) -> LendingMarket:
    return LendingMarket(
        protocol=_pick(row, "protocol"),
        symbol=(str(s).upper() if (s := _pick(row, "symbol")) else None),
        address=(str(a).lower() if (a := _pick(row, "address")) else None),
        supply_apr=_num(_pick(row, "supply_apr")),
        tvl_usd=_num(_pick(row, "tvl_usd")),
    )


def best_supply(markets: list[LendingMarket], *, symbol: str | None = None,
                address: str | None = None) -> LendingMarket | None:
    """Highest supply-APR market matching a token by symbol or address."""
    sym = symbol.upper() if symbol else None
    addr = address.lower() if address else None
    matches = [
        m for m in markets
        if m.supply_apr is not None
        and ((sym and m.symbol == sym) or (addr and m.address == addr))
    ]
    return max(matches, key=lambda m: m.supply_apr) if matches else None
