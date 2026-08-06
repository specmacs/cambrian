"""The defense: watch each position, rotate the losers, and pull to stables in a dump.

Two layers, matching the strategy:

  1. Per-position triggers (slow-bleed defense): a pool's APR decaying below a
     fraction of entry, its TVL draining (liquidity leaving = get out while you
     still can), or the volatile leg breaking a stop-loss → ROTATE out.

  2. Global circuit breaker (the cascade defense): when the market dumps or the
     whole book breaches its max drawdown, you don't rotate farm-to-farm —
     everything's bleeding — you flight-to-stables. That overrides everything;
     every non-stable position rotates to the reserve.

Pure logic, so every trigger is tested. Feeding it fresh APR/TVL/price and a
market signal is the caller's job (see the `monitor` command).
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import base_config
from ..base_config import MonitorPolicy


@dataclass(frozen=True)
class Holding:
    pool: str
    label: str
    usd: float
    is_stable: bool                 # stable/stable positions don't price-stop
    entry_apr: float | None = None
    current_apr: float | None = None
    entry_tvl: float | None = None
    current_tvl: float | None = None
    entry_price: float | None = None   # volatile-leg price at entry
    current_price: float | None = None


@dataclass(frozen=True)
class Trigger:
    pool: str
    label: str
    action: str                     # "HOLD" | "ROTATE"
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MonitorReport:
    triggers: tuple[Trigger, ...]
    go_stable: bool
    breaker_reasons: tuple[str, ...]

    @property
    def rotations(self) -> tuple[Trigger, ...]:
        return tuple(t for t in self.triggers if t.action == "ROTATE")


def check_position(h: Holding, policy: MonitorPolicy) -> Trigger:
    reasons: list[str] = []
    if h.entry_apr and h.current_apr is not None \
            and h.current_apr < policy.rotate_apr_frac * h.entry_apr:
        reasons.append(f"APR {h.current_apr:.0%} fell below "
                       f"{policy.rotate_apr_frac:.0%} of entry {h.entry_apr:.0%}")
    if h.entry_tvl and h.current_tvl is not None \
            and h.current_tvl < policy.rotate_tvl_frac * h.entry_tvl:
        reasons.append(f"TVL drained below {policy.rotate_tvl_frac:.0%} of entry")
    if not h.is_stable and h.entry_price and h.current_price is not None:
        drop = 1.0 - h.current_price / h.entry_price
        if drop >= policy.stop_loss_pct:
            reasons.append(f"token down {drop:.0%} from entry "
                           f"(stop {policy.stop_loss_pct:.0%})")
    return Trigger(h.pool, h.label, "ROTATE" if reasons else "HOLD", tuple(reasons))


def circuit_breaker(market_drawdown: float | None, portfolio_drawdown: float | None,
                    policy: MonitorPolicy) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if market_drawdown is not None and market_drawdown >= policy.market_dump_pct:
        reasons.append(f"market down {market_drawdown:.0%} "
                       f"(>= {policy.market_dump_pct:.0%}) — flight to stables")
    if portfolio_drawdown is not None and portfolio_drawdown >= policy.max_drawdown:
        reasons.append(f"book down {portfolio_drawdown:.0%} from high-water "
                       f"(>= {policy.max_drawdown:.0%}) — flight to stables")
    return bool(reasons), tuple(reasons)


def assess(holdings: list[Holding], *, market_drawdown: float | None = None,
           portfolio_drawdown: float | None = None,
           policy: MonitorPolicy | None = None) -> MonitorReport:
    policy = policy or base_config.MONITOR
    go_stable, breaker = circuit_breaker(market_drawdown, portfolio_drawdown, policy)
    if go_stable:
        # Cascade: every non-stable position rotates to the reserve.
        trigs = tuple(
            Trigger(h.pool, h.label,
                    "ROTATE" if not h.is_stable else "HOLD",
                    ("flight to stables",) if not h.is_stable else ())
            for h in holdings
        )
        return MonitorReport(trigs, True, breaker)
    return MonitorReport(tuple(check_position(h, policy) for h in holdings),
                         False, ())
