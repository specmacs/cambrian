"""Open positions: marking them honestly, and deciding when to get out.

Two decisions define this module, and both are corrections to how the desk used
to work.

**Mark at the SELL quote, never the mid price.** What a position is worth is what
you would actually receive for closing it — net of the creator tax, the protocol
fee, and the slippage of that specific size against that specific venue. On a
chain where 62% of flap launches carry a 10% tax and a fresh curve costs 5-11% in
price impact, a mid-price mark overstates every position materially and is exactly
the number that makes a desk feel profitable while it bleeds. `mark()` prices the
real exit.

**A failed sell quote marks to ZERO, never to the last good value.** This is
correction #1 in the handoff, and it must not regress: `mark_and_exit` once had
`if val is None: continue`, which preserved the last good mark forever when sell
quotes failed. A honeypot therefore showed a beautiful P&L right up until you
tried to leave. Here, an unquotable position marks at 0, increments `fails`, and
`RUG_FAILS` consecutive failures force a close.

Exit priority is the owner's, unchanged: stop -> trailing stop -> profit rungs ->
liquidity collapse -> time stop. The trailing stop is the part that lets a winner
run past the rungs instead of being trimmed to nothing.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace

from . import venues as V

# Exit policy. Same knobs and defaults the desk has always used.
TRAIL_ARM = float(os.getenv("RH_TRAIL_ARM", "1.35"))     # arm the trail at +35%
TRAIL_GIVE = float(os.getenv("RH_TRAIL_GIVE", "0.22"))   # give back 22% off peak
MAX_HOLD_MIN = float(os.getenv("RH_MAX_HOLD_MIN", "45"))
RUG_FAILS = int(os.getenv("RH_RUG_FAILS", "3"))
LIQ_COLLAPSE = float(os.getenv("RH_LIQ_COLLAPSE", "0.35"))
STOP_FRAC = float(os.getenv("RH_STOP_FRAC", "0.55"))     # hard stop at -45%
RUNGS: tuple[tuple[float, float], ...] = ((2.0, 0.50), (3.0, 0.25), (5.0, 0.15))


@dataclass
class Position:
    """One open position, valued in the settlement asset (USD terms)."""
    token: str
    venue_kind: str
    pool: str
    tokens: int                       # base units still held
    cost_usd: float                   # what was actually spent, all-in
    opened_at: float
    peak_usd: float = 0.0
    mark_usd: float | None = None
    fails: int = 0
    rungs_hit: list[int] = field(default_factory=list)
    liq0_usd: float | None = None
    closed: bool = False
    # False when the mark came from spot rather than a real sell quote (V3
    # venues). Surfaced in the UI so an approximate mark is never mistaken for a
    # priced exit.
    mark_is_exact: bool = True

    @property
    def multiple(self) -> float | None:
        if self.mark_usd is None or self.cost_usd <= 0:
            return None
        return self.mark_usd / self.cost_usd

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.opened_at) / 60.0


def open_position(*, token: str, venue: V.Venue, tokens: int, cost_usd: float,
                  now: float | None = None) -> Position:
    return Position(token=token, venue_kind=venue.kind, pool=venue.pool,
                    tokens=tokens, cost_usd=cost_usd,
                    opened_at=time.time() if now is None else now,
                    peak_usd=cost_usd)


def exit_value_usd(venue: V.Venue, tokens: int, *,
                   quote_price_usd: float) -> tuple[float | None, bool]:
    """(value_usd, is_exact) for closing this position right now.

    Three outcomes, and collapsing any two of them is a bug:

    1. **Exact.** `quote_sell` works (curves, V2 pairs), so tax, fee and the
       slippage of THIS size are all priced in. `is_exact` is True.
    2. **Approximate.** The venue has no constant-product reserves to quote from
       — a Uniswap V3 pool — but it does have an exact spot price. Mark at spot
       and flag it. Slightly optimistic, since spot ignores exit slippage, but
       vastly better than the alternative: treating "this venue type cannot be
       quoted locally" as "this position cannot be sold" manufactured a fake
       -100% and stopped out every Pons v1 position the instant it opened.
    3. **Unknown.** Neither works — the venue is broken or gone. Returns None so
       `mark` can treat it as the rug signal it is.
    """
    if tokens <= 0:
        return 0.0, True
    out = V.quote_sell(venue, tokens)
    if out is not None:
        return out / (10 ** venue.quote_decimals) * quote_price_usd, True
    spot = V.price_quote_per_token(venue)
    if spot:
        return (spot * (tokens / 10 ** venue.token_decimals) * quote_price_usd), False
    return None, False


def mark(pos: Position, venue: V.Venue, *, quote_price_usd: float,
         exit_quoter=None) -> Position:
    """Re-value a position at its real exit price.

    A failed quote marks to ZERO and counts a failure. Preserving the last good
    mark is how a honeypot renders as a winner — see correction #1.

    `exit_quoter(venue, tokens) -> (usd, status)` is required to close a hole that
    the spot fallback opened. On a venue with no local sell quote, spot ALWAYS
    returns a number, so `fails` never increments and the rug check can never
    fire — a V3 or v4 honeypot would mark healthy forever, because spot reflects
    the pool's price rather than whether you personally can sell. When a quoter is
    supplied it is authoritative for those venues: status "unsellable" drives the
    rug path, "error" leaves the mark alone (a network blip is not a rug, and
    closing good positions on a bad connection is its own kind of loss).
    """
    val, exact = exit_value_usd(venue, pos.tokens, quote_price_usd=quote_price_usd)
    if val is None:
        pos.fails += 1
        pos.mark_usd = 0.0
        return pos
    if not exact and exit_quoter is not None:
        quoted, status = exit_quoter(venue, pos.tokens)
        if status == "unsellable":
            pos.fails += 1
            pos.mark_usd = 0.0
            return pos
        if status == "ok" and quoted is not None:
            val, exact = quoted, True
    pos.fails = 0
    pos.mark_usd = val
    pos.mark_is_exact = exact
    pos.peak_usd = max(pos.peak_usd, val)
    return pos


def exit_decision(pos: Position, *, liquidity_usd: float | None = None,
                  now: float | None = None) -> tuple[str | None, float, str]:
    """(action, fraction, why) — action is 'close', 'trim' or None.

    Priority order is deliberate and unchanged: a stop must beat a rung, and a
    rug must beat both.
    """
    t = time.time() if now is None else now
    if pos.closed:
        return None, 0.0, "already closed"

    # 0. Cannot be sold, repeatedly — treat as a rug and get out at any price.
    if pos.fails >= RUG_FAILS:
        return "close", 1.0, "unsellable x%d — rug" % pos.fails

    if pos.mark_usd is None:
        return None, 0.0, "unmarked"
    mult = pos.mark_usd / pos.cost_usd if pos.cost_usd > 0 else 0.0
    peak_mult = pos.peak_usd / pos.cost_usd if pos.cost_usd > 0 else 0.0

    # 1. Hard stop.
    if mult <= STOP_FRAC:
        return "close", 1.0, "stop %.0f%%" % ((mult - 1) * 100)

    # 2. Trailing stop — what keeps a +80% winner from round-tripping to flat.
    if peak_mult >= TRAIL_ARM and pos.mark_usd <= pos.peak_usd * (1 - TRAIL_GIVE):
        return "close", 1.0, "trail %.0f%% off %.1fx" % (TRAIL_GIVE * 100, peak_mult)

    # 3. Profit rungs — bank on the way up, keep a moon bag.
    for i, (at, frac) in enumerate(RUNGS):
        if mult >= at and i not in pos.rungs_hit:
            pos.rungs_hit.append(i)
            return "trim", frac, "rung %gx" % at

    # 4. Liquidity pulled out from under us.
    if liquidity_usd is not None:
        if pos.liq0_usd is None:
            pos.liq0_usd = max(liquidity_usd, 1e-9)
        elif liquidity_usd < pos.liq0_usd * LIQ_COLLAPSE:
            return "close", 1.0, "liquidity -%.0f%%" % (
                100 * (1 - liquidity_usd / pos.liq0_usd))

    # 5. Time stop, only if nothing has been banked yet.
    if (t - pos.opened_at) / 60.0 > MAX_HOLD_MIN and not pos.rungs_hit:
        return "close", 1.0, "time stop %.0fm" % MAX_HOLD_MIN
    return None, 0.0, "hold"


def apply(pos: Position, action: str | None, fraction: float) -> Position:
    """Reduce a position by an executed action. Returns the updated position.

    Applied only after the trade actually fills — calling this on intent would
    make the book disagree with the chain, which is worse than not tracking at
    all.
    """
    if not action:
        return pos
    if action == "close" or fraction >= 1.0:
        return replace(pos, tokens=0, closed=True)
    sold = int(pos.tokens * fraction)
    return replace(pos, tokens=max(pos.tokens - sold, 0),
                   cost_usd=pos.cost_usd * (1 - fraction))


def portfolio_summary(positions: list[Position]) -> dict:
    """Book-level view. Realised P&L is not tracked here — only open exposure."""
    live = [p for p in positions if not p.closed]
    cost = sum(p.cost_usd for p in live)
    marked = sum(p.mark_usd for p in live if p.mark_usd is not None)
    unmarked = sum(1 for p in live if p.mark_usd is None)
    return {
        "open": len(live),
        "cost_usd": cost,
        "value_usd": marked,
        "pnl_usd": marked - cost,
        "pnl_pct": (100.0 * (marked - cost) / cost) if cost else None,
        "unmarked": unmarked,
        "at_risk": sum(1 for p in live if p.fails > 0),
    }
