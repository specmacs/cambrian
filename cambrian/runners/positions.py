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

# --- Exit policy -------------------------------------------------------------
#
# Retuned for what these launches actually are, using numbers measured live this
# session rather than the generic defaults the desk started with.
#
# **The anchor is that a round trip costs ~5%** (2.6% in, similar out, on an $8
# ticket into a $45k market cap). `mult` is exit-value over cost, so a position
# marks at ~0.95 the instant it fills and a flat token never reads 1.00. Every
# threshold below is stated in those terms, because reading them as token moves
# is off by the entry cost in the dangerous direction.
#
# The shape of the bet: most fresh launches go to zero, a few run hard. That
# asymmetry says cut fast and bank early, then let the remainder run — which is
# exactly the owner's "farm profit, or stop out fast if the entry is shit".

# Hard stop at -20% on the position (~-15% on the token, after the 5% entry).
# Was -45%, which on an $8 ticket is $3.60 gone before the desk reacts — far too
# patient for something that resolves in minutes.
STOP_FRAC = float(os.getenv("RH_STOP_FRAC", "0.80"))

# Arm the trail at +25% and give back 30% of the peak. Armed EARLIER than before
# (1.35 -> 1.25) so a spike-and-fade does not round-trip to flat, but with MORE
# room (0.22 -> 0.30) because these move violently and a 22% pullback is noise,
# not a reversal. Spike to 1.25 then fade exits at ~0.88 — ahead of the stop.
TRAIL_ARM = float(os.getenv("RH_TRAIL_ARM", "1.25"))
TRAIL_GIVE = float(os.getenv("RH_TRAIL_GIVE", "0.30"))

# Bank a third at +40%, which is where the common winner actually tops out, and
# it clears the round trip several times over. The old ladder started at 2x and
# banked NOTHING on every launch that ran 40-90% and faded — the modal good
# outcome. 22% rides to whatever it becomes.
RUNGS: tuple[tuple[float, float], ...] = ((1.4, 0.33), (2.5, 0.25), (5.0, 0.20))

# 8 minutes, not 45. Blocks are 100ms and these resolve in minutes; if nothing
# has been banked by then the thesis was wrong and the capital is better used on
# the next launch. Only fires when no rung has hit, so a winner is never cut by
# the clock.
MAX_HOLD_MIN = float(os.getenv("RH_MAX_HOLD_MIN", "8"))

# **Stall exit — the one that keeps capital turning over.** A position that has
# not made a new high in this long has no buyers behind it, and on a fresh launch
# that is the whole thesis gone. The desk is meant to be trading at a fairly
# consistent rate, not buying and holding: dead volume is a sell, not a wait. 90s
# is deliberately short — these resolve in minutes, and the alternative to
# holding a flat bag is the next launch.
STALL_S = float(os.getenv("RH_STALL_S", "90"))

RUG_FAILS = int(os.getenv("RH_RUG_FAILS", "3"))
LIQ_COLLAPSE = float(os.getenv("RH_LIQ_COLLAPSE", "0.35"))


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
    # When the peak was last set. A position making new highs is alive; one that
    # is not is a bag, and telling those apart needs a clock, not a price.
    peak_at: float = 0.0
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
    if val > pos.peak_usd:
        pos.peak_usd, pos.peak_at = val, time.time()
    return pos


def mark_from_exit_quote(pos: Position, value_usd: float | None) -> Position:
    """Mark a position at what a real sell quote says it would pay.

    For a position held in an execution venue's own account, this is strictly
    better than `mark()`: it needs no pool, no reserves and no venue resolution,
    and the number IS the exit price rather than a model of it — tax, route and
    the slippage of this exact size are all already inside it. So `mark_is_exact`
    is True, not aspirationally but by construction.

    `None` means the venue would not quote a sell, which is the same unsellable
    signal `mark()` treats as a rug: fails increments and the mark goes to zero.
    Preserving the last good mark is how a honeypot renders as a winner.
    """
    if value_usd is None:
        pos.fails += 1
        pos.mark_usd = 0.0
        return pos
    pos.fails = 0
    pos.mark_usd = value_usd
    pos.mark_is_exact = True
    if value_usd > pos.peak_usd:
        pos.peak_usd, pos.peak_at = value_usd, time.time()
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

    # 4b. Stall — no new high for STALL_S. No buyers, no thesis; recycle the
    # capital into the next launch rather than sitting in a flat bag. Skipped
    # once a rung has banked, so a winner that is consolidating is left alone.
    since_peak = t - (pos.peak_at or pos.opened_at)
    if not pos.rungs_hit and since_peak > STALL_S:
        return "close", 1.0, "stalled %.0fs — no new high" % since_peak

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
