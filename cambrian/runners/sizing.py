"""Position sizing: a standard buy expressed as a fraction of market cap.

Sizing off market cap rather than off pool depth is the right default because it
scales with the OPPORTUNITY instead of with the venue's plumbing. A $5k-FDV
launch and a $500k-FDV runner deserve very different tickets, and depth-based
sizing gets that backwards on this chain — every flap launch is seeded with an
identical 1.919 WETH of (virtual) reserve, so depth carries almost no information
about which launch is worth more.

Three clamps sit on top of the headline rule, in this order:

1. **Bankroll cap.** Never risk more than `risk_bps` of the account on one
   ticket, whatever the market cap says.
2. **Absolute floor/ceiling.** Below the floor, gas and fees dominate and the
   trade is not worth placing; above the ceiling you are the market.
3. **Slippage cap.** Solve for the largest size whose round-trip slippage stays
   under `max_slippage_bps`. This is the one that matters most early: measured on
   live Pons v2 curves, 0.1 ETH into a young curve cost 5-11% in price impact
   alone, dwarfing a 1% fee. Tax you screen for; slippage you size for.

The clamps are deliberately separate from the tax gate. Tax decides WHETHER to
trade (`venues.tradeable`); size decides HOW MUCH. Folding them together hides
which one rejected a setup.
"""

from __future__ import annotations

import os

from . import venues as V

BPS = 10_000

# Defaults tuned for a small, aggressive book. Override via env.
BANKROLL_USD = float(os.getenv("RH_BANKROLL_USD", "1000"))
BUY_BPS_OF_MC = int(os.getenv("RH_BUY_BPS_OF_MC", "50"))        # 0.50% of FDV
RISK_BPS_OF_BANKROLL = int(os.getenv("RH_RISK_BPS", "500"))     # 5% per ticket
MIN_BUY_USD = float(os.getenv("RH_MIN_BUY_USD", "10"))
MAX_BUY_USD = float(os.getenv("RH_MAX_BUY_USD", "100"))
MAX_SLIPPAGE_BPS = int(os.getenv("RH_MAX_SLIPPAGE_BPS", "300"))  # 3% round trip


def target_size_usd(mc_usd: float | None, *, bankroll_usd: float | None = None,
                    bps_of_mc: int | None = None, risk_bps: int | None = None,
                    min_usd: float | None = None,
                    max_usd: float | None = None) -> float | None:
    """The standard ticket for a token at `mc_usd`, before slippage capping.

    Returns None on an unknown market cap — sizing blind is how a $5k launch gets
    the ticket meant for a $500k one.
    """
    if mc_usd is None or mc_usd <= 0:
        return None
    bankroll = BANKROLL_USD if bankroll_usd is None else bankroll_usd
    size = mc_usd * (BUY_BPS_OF_MC if bps_of_mc is None else bps_of_mc) / BPS
    size = min(size, bankroll * (RISK_BPS_OF_BANKROLL if risk_bps is None else risk_bps) / BPS)
    size = min(size, MAX_BUY_USD if max_usd is None else max_usd)
    floor = MIN_BUY_USD if min_usd is None else min_usd
    return size if size >= floor else 0.0


def slippage_bps_for(venue: V.Venue, quote_in: int) -> int | None:
    """Round-trip price impact of `quote_in`, with fees and tax stripped out.

    Isolating slippage matters because it is the part sizing can fix; the fee/tax
    part is constant at any size and belongs to the gate instead.
    """
    free = V.Venue(**{**venue.__dict__, "fee_bps": 0, "tax_bps": 0})
    rt = V.round_trip(free, quote_in)
    return None if rt is None else rt["total_loss_bps"]


def cap_for_slippage(venue: V.Venue, want_quote: int, *,
                     max_slippage_bps: int | None = None,
                     steps: int = 24) -> int:
    """Largest input <= `want_quote` whose round-trip slippage clears the cap.

    Binary search rather than a closed form: the constant-product inverse is
    solvable, but the search also respects the `sellableTokens` clamp and any
    future venue quirk without needing a second derivation per venue.
    """
    limit = MAX_SLIPPAGE_BPS if max_slippage_bps is None else max_slippage_bps
    if want_quote <= 0:
        return 0
    # Explicit None check, NOT `or`: a deep venue legitimately returns 0 bps of
    # slippage, and `0 or BPS` would treat the best possible case as the worst.
    full = slippage_bps_for(venue, want_quote)
    if full is not None and full <= limit:
        return want_quote
    lo, hi = 0, want_quote
    for _ in range(steps):
        mid = (lo + hi) // 2
        if mid <= 0:
            break
        s = slippage_bps_for(venue, mid)
        if s is not None and s <= limit:
            lo = mid
        else:
            hi = mid
    return lo


def plan_entry(venue: V.Venue, *, quote_price_usd: float,
               bankroll_usd: float | None = None,
               max_slippage_bps: int | None = None) -> dict:
    """The full entry decision for one token: gate, size, cost, and why.

    One call gives everything needed to act or to explain not acting. Every
    rejection carries its reason, because "no trade" without a reason is
    indistinguishable from a broken feed — which is exactly how pons and flap sat
    unwatched for weeks.
    """
    gate = V.tradeable(venue)
    mc_usd = V.market_cap_usd(venue, quote_price_usd)
    out: dict = {
        "token": venue.token, "venue": venue.kind, "ok": False,
        "reasons": list(gate["reasons"]),
        "market_cap_usd": mc_usd,
        "price_quote": V.price_quote_per_token(venue),
        "tax_bps": venue.tax_bps, "fee_bps": venue.fee_bps,
        "size_usd": None, "quote_in": None, "slippage_bps": None,
        "round_trip": None, "prices_off_virtual_reserves": venue.prices_off_virtual_reserves,
    }
    want_usd = target_size_usd(mc_usd, bankroll_usd=bankroll_usd)
    if want_usd is None:
        out["reasons"].append("unknown market cap")
        return out
    if want_usd <= 0:
        out["reasons"].append("below minimum ticket")
        return out
    scale = 10 ** venue.quote_decimals
    want_quote = int(want_usd / quote_price_usd * scale)

    # Concentrated-liquidity venues (Pons v1 on Uniswap V3) have an exact spot
    # price but no constant-product reserves, so there is no honest local
    # round-trip to solve for. Rather than fake x*y=k on a V3 pool — which
    # misprices depth in both directions — size off market cap and let Flash
    # enforce slippage at execution, where it does the tick math properly.
    # `execution.prepare` re-checks the real quote before anything is signable.
    if V.quote_buy(venue, want_quote) is None:
        out.update({"size_usd": want_quote / scale * quote_price_usd,
                    "quote_in": want_quote,
                    "slippage_priced_by": "flash-at-execution"})
        out["ok"] = not out["reasons"]
        return out

    capped = cap_for_slippage(venue, want_quote, max_slippage_bps=max_slippage_bps)
    if capped <= 0:
        out["reasons"].append("no size clears the slippage cap")
        return out
    rt = V.round_trip(venue, capped)
    out.update({
        "size_usd": capped / scale * quote_price_usd,
        "quote_in": capped,
        "slippage_bps": slippage_bps_for(venue, capped),
        "round_trip": rt,
        "capped_by_slippage": capped < want_quote,
    })
    out["ok"] = not out["reasons"]
    return out
