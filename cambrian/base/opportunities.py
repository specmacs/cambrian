"""One ranked view of the whole yield field: LP pools + lending markets together.

The scanner ranks LP pools; the allocator compares LP vs lending per token. This
merges both into a single "here's the best yield anywhere on Base right now" list
— LP fees+emissions and lending supply rates side by side, ranked by APY, filtered
for depth. Pure; the CLI feeds it the paginated pool set and the lending overview.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..base.lending import LendingMarket
from ..base.pools import PoolYield


@dataclass(frozen=True)
class Opportunity:
    kind: str            # "LP" | "LEND"
    venue: str           # DEX or lending protocol
    label: str           # pair (WETH/USDC) or asset (USDC)
    apr: float           # all-in APR as a fraction
    tvl_usd: float | None
    detail: str


def build_opportunities(pools: list[PoolYield], markets: list[LendingMarket], *,
                        min_tvl_usd: float, max_results: int = 40) -> list[Opportunity]:
    out: list[Opportunity] = []

    for p in pools:
        if p.fee_apr is None or p.tvl_usd is None or p.tvl_usd < min_tvl_usd:
            continue
        if p.fee_apr and p.swap_fee_apr is not None:
            share = p.swap_fee_apr / p.fee_apr if p.fee_apr else 0
            detail = f"{share:.0%} real fees"
        else:
            detail = "fees"
        out.append(Opportunity("LP", p.dex, p.label, p.fee_apr, p.tvl_usd, detail))

    for m in markets:
        if m.supply_apr is None:
            continue
        if m.tvl_usd is not None and m.tvl_usd < min_tvl_usd:
            continue
        out.append(Opportunity("LEND", m.protocol or "lending",
                               m.symbol or "?", m.supply_apr, m.tvl_usd, "supply"))

    out.sort(key=lambda o: o.apr, reverse=True)
    return out[:max_results]
