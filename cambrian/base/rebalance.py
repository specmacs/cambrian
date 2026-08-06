"""The babysitter: what to change to get from where you are to the new target.

Given your current positions and a freshly-built target portfolio, emit the
minimal set of ENTER / EXIT / RESIZE actions. Small drifts are ignored — churning
a position for a 2% weight change just pays fees and MEV for nothing. Pure; the
actual moves are journaled and (for now) simulated.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..base.portfolio import Portfolio


@dataclass(frozen=True)
class Action:
    kind: str          # "ENTER" | "EXIT" | "RESIZE"
    pool: str
    label: str
    from_usd: float
    to_usd: float

    @property
    def delta_usd(self) -> float:
        return round(self.to_usd - self.from_usd, 2)


def plan_rebalance(current: dict[str, float], target: Portfolio,
                   *, min_move_usd: float = 25.0) -> list[Action]:
    """`current` maps pool address -> current USD. Returns actions to reach target."""
    target_usd = {p.pool: p.usd for p in target.positions}
    labels = {p.pool: p.label for p in target.positions}
    actions: list[Action] = []

    # Exits: held but not in target (or target zero).
    for pool, cur in current.items():
        if cur > 0 and target_usd.get(pool, 0.0) <= 0:
            actions.append(Action("EXIT", pool, labels.get(pool, pool), cur, 0.0))

    # Enters and resizes.
    for pool, tgt in target_usd.items():
        cur = current.get(pool, 0.0)
        if cur <= 0 and tgt > 0:
            actions.append(Action("ENTER", pool, labels.get(pool, pool), 0.0, tgt))
        elif cur > 0 and abs(tgt - cur) >= min_move_usd:
            actions.append(Action("RESIZE", pool, labels.get(pool, pool), cur, tgt))

    return actions
