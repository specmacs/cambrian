"""LP-vs-lending allocator.

For a token, is providing liquidity actually worth it versus just lending the
token out? LP earns fees but eats impermanent loss; lending earns a flat supply
rate with no IL. This compares the two on a like-for-like, IL-haircut basis and
recommends one — a pure function, so it's testable without the network.

The haircut is deliberately blunt: subtract the impermanent loss of a stress-
sized move from the LP APR as a one-time annual penalty. It's a conservative
tiebreaker, not a precise expected-value model — LP has to clear lending by more
than the stress IL to win.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..units import impermanent_loss


@dataclass(frozen=True)
class Allocation:
    token: str
    choice: str          # "LP" | "LEND" | "NONE"
    lp_apr: float | None
    lp_net_apr: float | None   # lp_apr minus the stress IL haircut
    lend_apr: float | None
    il_haircut: float
    reason: str


def recommend(token: str, lp_apr: float | None, lend_apr: float | None,
              *, stress_price_ratio: float = 1.5) -> Allocation:
    il = impermanent_loss(stress_price_ratio)
    lp_net = (lp_apr - il) if lp_apr is not None else None

    if lp_net is None and lend_apr is None:
        return Allocation(token, "NONE", lp_apr, lp_net, lend_apr, il,
                          "no LP or lending yield found for this token")
    if lp_net is None:
        return Allocation(token, "LEND", lp_apr, lp_net, lend_apr, il,
                          "no LP pool cleared; lending is the only yield")
    if lend_apr is None:
        return Allocation(token, "LP", lp_apr, lp_net, lend_apr, il,
                          "no lending market found; LP is the only yield")

    if lp_net > lend_apr:
        return Allocation(token, "LP", lp_apr, lp_net, lend_apr, il,
                          f"LP net {lp_net:.2%} beats lending {lend_apr:.2%} "
                          f"after {il:.2%} stress-IL haircut")
    return Allocation(token, "LEND", lp_apr, lp_net, lend_apr, il,
                      f"lending {lend_apr:.2%} beats LP net {lp_net:.2%} "
                      f"(LP {lp_apr:.2%} minus {il:.2%} stress-IL)")
