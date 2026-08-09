"""The live loop: discover on one cadence, mark and exit on a faster one.

Everything else in this package answers a question once. This runs it forever,
which introduces the only genuinely new failure mode left: a position that is
never re-valued, or an exit that fires late because the loop was busy scanning.

**Two cadences, deliberately.** Discovery is slow — a sweep touches every pad,
resolves venues and prices quote assets. Marking is fast and matters more: a stop
that fires 30 seconds late on a memecoin is a stop that did not fire. So marking
runs on its own short interval and never waits for a scan. This is correction #4
in the handoff, which moved valuation off the tail of the slow scan for exactly
this reason.

**Dry-run is the default and execution is opt-in.** `tick()` returns intents. It
does not sign, submit, or mutate a position on intent — `positions.apply` is
called only on a real fill, because a book that updates on intent disagrees with
the chain, which is worse than not tracking at all.

**Discovery is deduplicated by token, and the seen-set is bounded.** A launch
seen once must not re-alert every scan, and an unbounded set is a slow leak in a
process meant to run for days.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import config as rcfg
from . import positions as P
from . import scanner
from . import stock_tokens as ST
from . import venues as V

SCAN_INTERVAL_S = float(os.getenv("RH_SCAN_INTERVAL_S", "20"))
MARK_INTERVAL_S = float(os.getenv("RH_MARK_INTERVAL_S", "2"))
SCAN_BLOCKS = int(os.getenv("RH_SCAN_BLOCKS", "1200"))
SEEN_MAX = int(os.getenv("RH_SEEN_MAX", "5000"))


@dataclass
class LiveState:
    positions: dict[str, P.Position] = field(default_factory=dict)
    venues: dict[str, V.Venue] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)      # ordered, for bounded eviction
    _seen_set: set[str] = field(default_factory=set)
    last_scan_at: float = 0.0
    last_mark_at: float = 0.0
    scans: int = 0
    marks: int = 0

    def note_seen(self, token: str) -> bool:
        """True if this token is new. Bounds the set so a long run cannot leak."""
        key = token.lower()
        if key in self._seen_set:
            return False
        self._seen_set.add(key)
        self.seen.append(key)
        if len(self.seen) > SEEN_MAX:
            drop = self.seen.pop(0)
            self._seen_set.discard(drop)
        return True


def scan_tick(client, state: LiveState, *, quote_price_usd: float,
              bankroll_usd: float | None = None, blocks: int = SCAN_BLOCKS,
              now: float | None = None) -> list[dict]:
    """Discover fresh launches and return only the NEW, tradeable ones."""
    t = time.time() if now is None else now
    latest = client.block_number()
    result = scanner.sweep(client, from_block=max(latest - blocks, 0),
                           to_block=latest, quote_price_usd=quote_price_usd,
                           bankroll_usd=bankroll_usd)
    state.last_scan_at, state.scans = t, state.scans + 1
    fresh = []
    for row in result["rows"]:
        token = row.get("token")
        if not token or not state.note_seen(token):
            continue
        if row.get("ok"):
            fresh.append(row)
    return fresh


def mark_tick(client, state: LiveState, *, weth_usd: float,
              now: float | None = None) -> list[dict]:
    """Re-value every open position and return the exit intents.

    Returns intents rather than acting. Nothing here mutates a position: the book
    changes only when a fill comes back.
    """
    t = time.time() if now is None else now
    state.last_mark_at, state.marks = t, state.marks + 1
    intents: list[dict] = []
    for token, pos in list(state.positions.items()):
        if pos.closed:
            continue
        venue = state.venues.get(token)
        if venue is None:
            continue
        qp = ST.quote_price_usd(venue.quote_token, weth_usd=weth_usd,
                                usdg=rcfg.CONTRACTS.get("usdg"),
                                weth=rcfg.CONTRACTS.get("weth"))
        # An unpriceable quote asset is not a zero mark — it is an unknown one,
        # and `mark` already treats a failed valuation as a rug signal. Skipping
        # here would instead freeze the mark, which is the bug correction #1 was.
        P.mark(pos, venue, quote_price_usd=qp if qp is not None else weth_usd)
        action, fraction, why = P.exit_decision(pos, now=t)
        if action:
            intents.append({"token": token, "action": action, "fraction": fraction,
                            "why": why, "mark_usd": pos.mark_usd,
                            "cost_usd": pos.cost_usd, "venue": venue.kind})
    return intents


def due(state: LiveState, *, now: float | None = None) -> tuple[bool, bool]:
    """(scan_due, mark_due). Marking is checked independently of scanning.

    A stop that fires 30 seconds late on a memecoin is a stop that did not fire,
    so marking must never wait on a slow sweep.
    """
    t = time.time() if now is None else now
    return ((t - state.last_scan_at) >= SCAN_INTERVAL_S,
            (t - state.last_mark_at) >= MARK_INTERVAL_S)


def record_fill(state: LiveState, *, token: str, venue: V.Venue, tokens: int,
                cost_usd: float, now: float | None = None) -> P.Position:
    """Open or add to a position AFTER a fill actually settled."""
    pos = P.open_position(token=token, venue=venue, tokens=tokens,
                          cost_usd=cost_usd, now=now)
    state.positions[token] = pos
    state.venues[token] = venue
    return pos


def record_exit_fill(state: LiveState, *, token: str, action: str,
                     fraction: float) -> P.Position | None:
    """Apply an exit that actually filled."""
    pos = state.positions.get(token)
    if pos is None:
        return None
    state.positions[token] = P.apply(pos, action, fraction)
    return state.positions[token]


def format_tick(fresh: list[dict], intents: list[dict], state: LiveState) -> str:
    """One compact block per tick — only when something happened."""
    lines: list[str] = []
    for row in fresh:
        lines.append("  NEW  %-9s %-12s %-6s MC $%-9s tax %-6s size $%.2f" % (
            row.get("pad", "?"), (row.get("token") or "")[:12],
            scanner._quote_label(row),
            ("%.0f" % row["market_cap_usd"]) if row.get("market_cap_usd") else "?",
            ("%.1f%%" % (row["tax_bps"] / 100)) if row.get("tax_bps") is not None else "n/a",
            row.get("size_usd") or 0.0))
    for i in intents:
        pnl = (100 * (i["mark_usd"] / i["cost_usd"] - 1)) if i["cost_usd"] else 0.0
        lines.append("  EXIT %-5s %-12s %3.0f%% of position — %-22s (%+.1f%%)" % (
            i["action"].upper(), i["token"][:12], i["fraction"] * 100, i["why"], pnl))
    if not lines:
        return ""
    summary = P.portfolio_summary(list(state.positions.values()))
    lines.append("       book: %d open, $%.2f cost, $%.2f value (%s)"
                 % (summary["open"], summary["cost_usd"], summary["value_usd"],
                    ("%+.1f%%" % summary["pnl_pct"]) if summary["pnl_pct"] is not None else "n/a"))
    return "\n".join(lines)
