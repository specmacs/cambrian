"""The brain: turn "$X" into a concrete target portfolio.

Given scored pools and a deposit, allocate across a low-risk core sleeve and a
capped high-yield satellite sleeve, weighting by risk-adjusted score, clamped by
per-pool and per-token concentration limits, with the remainder held as a stable
reserve. Deterministic and pure — no network, no keys — so you can eyeball its
picks before a cent moves.

Clamping is conservative: anything a cap trims off doesn't get force-fed into the
next pool, it falls to the reserve. Better to hold dry powder than over-concentrate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import base_config
from ..base_config import BaseLPPolicy
from ..base.score import PoolScore


@dataclass(frozen=True)
class Position:
    dex: str
    pool: str
    label: str
    usd: float
    weight: float          # fraction of total capital
    sleeve: str
    expected_apr: float    # risk-adjusted net APR (the score)
    reason: str


@dataclass(frozen=True)
class Portfolio:
    capital_usd: float
    positions: tuple[Position, ...]
    reserve_usd: float
    blended_apr: float     # capital-weighted, reserve counted at 0

    @property
    def deployed_usd(self) -> float:
        return sum(p.usd for p in self.positions)


def _allocate(cands: list[PoolScore], budget: float, capital: float,
              policy: BaseLPPolicy, token_usd: dict[str, float],
              max_positions: int) -> list[tuple[PoolScore, float]]:
    cap_pool = policy.max_per_pool * capital
    cap_tok = policy.max_per_token * capital

    # Select up to max_positions by score, skipping ones that would blow a
    # per-token cap (shared token exposure across pools).
    selected: list[PoolScore] = []
    for c in cands:
        if len(selected) >= max_positions:
            break
        toks = [a for a in c.pool.token_addrs if a]
        if any(token_usd.get(a, 0.0) >= cap_tok for a in toks):
            continue
        selected.append(c)
    if not selected:
        return []

    total = sum(max(c.score, 1e-9) for c in selected)
    out: list[tuple[PoolScore, float]] = []
    for c in selected:
        usd = budget * (max(c.score, 1e-9) / total)
        usd = min(usd, cap_pool)
        for a in (x for x in c.pool.token_addrs if x):
            usd = min(usd, cap_tok - token_usd.get(a, 0.0))
        if usd < 1.0:
            continue
        for a in (x for x in c.pool.token_addrs if x):
            token_usd[a] = token_usd.get(a, 0.0) + usd
        out.append((c, usd))
    return out


def build_portfolio(capital_usd: float, scored: list[PoolScore],
                    policy: BaseLPPolicy | None = None) -> Portfolio:
    policy = policy or base_config.BASE_POLICY
    core = sorted((s for s in scored if s.sleeve == "core"),
                  key=lambda s: s.score, reverse=True)
    sat = sorted((s for s in scored if s.sleeve == "satellite"),
                 key=lambda s: s.score, reverse=True)

    token_usd: dict[str, float] = {}
    picks = _allocate(core, policy.core_target * capital_usd, capital_usd, policy,
                      token_usd, policy.max_core_positions)
    picks += _allocate(sat, policy.satellite_target * capital_usd, capital_usd, policy,
                       token_usd, policy.max_satellite_positions)

    positions = tuple(
        Position(dex=c.pool.dex, pool=c.pool.address, label=c.pool.label,
                 usd=round(usd, 2), weight=usd / capital_usd, sleeve=c.sleeve,
                 expected_apr=c.score,
                 reason="; ".join(c.reasons))
        for c, usd in picks
    )
    deployed = sum(p.usd for p in positions)
    reserve = round(capital_usd - deployed, 2)
    blended = (sum(p.usd * p.expected_apr for p in positions) / capital_usd
               if capital_usd else 0.0)
    return Portfolio(capital_usd, positions, reserve, blended)
