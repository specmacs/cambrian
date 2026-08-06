"""The autonomous cycle: combine a fresh target with the monitor's verdict, then
emit the moves to execute.

One cycle:
  1. fresh target portfolio (scan → score → allocate)
  2. the monitor's read on what we currently hold
  3. reconcile: if the breaker fired, the target becomes all-stables (exit
     everything); otherwise any position the monitor flagged to rotate is dropped
     from the target so it gets exited instead of held
  4. diff current → reconciled target into ENTER/EXIT/RESIZE actions

`plan_cycle` is pure — the whole decision is testable. Turning the actions into
real positions is the executor's job (paper today; live is the gated seam).
"""

from __future__ import annotations

from dataclasses import replace

from ..base.monitor import MonitorReport
from ..base.portfolio import Portfolio
from ..base.rebalance import Action, plan_rebalance


def plan_cycle(target: Portfolio, current: dict[str, float],
               report: MonitorReport) -> tuple[Portfolio, list[Action]]:
    """Return the reconciled target and the actions to reach it from `current`."""
    if report.go_stable:
        final = replace(target, positions=(), reserve_usd=target.capital_usd,
                        blended_apr=0.0)
    else:
        rotated = {t.pool for t in report.rotations}
        if rotated:
            kept = tuple(p for p in target.positions if p.pool not in rotated)
            deployed = sum(p.usd for p in kept)
            final = replace(target, positions=kept,
                            reserve_usd=round(target.capital_usd - deployed, 2))
        else:
            final = target
    return final, plan_rebalance(current, final)
