"""Score a fresh token: is this actually running, and is it a rug?

Two gates, like everything else in this repo. First the hard rug/quality filters
(fail closed — an unknown top-holder or liquidity is a reject, not a maybe).
Then, for survivors, a weighted signal score: smart-money buying (the big one),
volume acceleration, holder growth, buy/sell skew. Output is a tier — hot / watch
/ cold — with the reasons, so you see *why* it's flagged, not just a number.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import config as chain_cfg
from ..units import addr_in
from . import config as rcfg
from .config import RunnerPolicy
from .snapshots import RunnerCandidate


@dataclass(frozen=True)
class RunnerScore:
    candidate: RunnerCandidate
    score: float
    tier: str                    # "hot" | "watch" | "cold" | "reject"
    reasons: tuple[str, ...]     # why rejected (empty unless reject)
    signals: tuple[str, ...]     # positive drivers


def score_runner(c: RunnerCandidate, *, policy: RunnerPolicy | None = None,
                 trusted_launchpads=None) -> RunnerScore:
    policy = policy or rcfg.RUNNER
    trusted = (chain_cfg.TRUSTED_FACTORIES if trusted_launchpads is None
               else trusted_launchpads)

    # --- Hard filters: freshness, depth, rug ---
    reasons: list[str] = []
    if c.age_minutes is None or c.age_minutes > policy.max_age_minutes:
        reasons.append("not fresh (age unknown or too old)")
    if c.liquidity_usd is None or c.liquidity_usd < policy.min_liquidity_usd:
        reasons.append(f"liquidity below ${policy.min_liquidity_usd:,.0f}")
    if c.holders is None or c.holders < policy.min_holders:
        reasons.append(f"under {policy.min_holders} holders")
    if c.top_holder_pct is None or c.top_holder_pct > policy.max_top_holder_pct:
        reasons.append("top-holder concentration too high / unknown")
    if c.lp_locked is False:
        reasons.append("LP not locked")
    if policy.require_trusted_launchpad and not addr_in(c.launchpad, trusted):
        reasons.append("launchpad not in trusted set")
    if reasons:
        return RunnerScore(c, 0.0, "reject", tuple(reasons), ())

    # --- Signals ---
    score = 0.0
    signals: list[str] = []

    sm = min(c.smart_money_buyers / 3.0, 1.0)
    score += policy.w_smart_money * sm
    if c.smart_money_buyers > 0:
        signals.append(f"{c.smart_money_buyers} smart wallet(s) in")

    if c.volume_prior_5m_usd and c.volume_5m_usd is not None:
        ratio = c.volume_5m_usd / max(c.volume_prior_5m_usd, 1e-9)
        score += policy.w_volume * min(ratio / policy.vol_accel_hot, 1.0)
        if ratio >= policy.vol_accel_hot:
            signals.append(f"volume {ratio:.1f}x accelerating")

    if c.holders_5m_ago and c.holders is not None:
        growth = (c.holders - c.holders_5m_ago) / max(c.holders_5m_ago, 1)
        score += policy.w_holders * min(max(growth, 0.0), 1.0)
        if growth > 0.2:
            signals.append(f"holders +{growth:.0%}/5m")

    if c.buys_5m is not None and c.sells_5m is not None \
            and (c.buys_5m + c.sells_5m) > 0:
        skew = c.buys_5m / (c.buys_5m + c.sells_5m)
        score += policy.w_buy_skew * min(max((skew - 0.5) * 2, 0.0), 1.0)
        if skew > 0.65:
            signals.append(f"{skew:.0%} buys")

    # --- Gaming-resistant overlays (neutral when the fact is unknown/None) ---
    # Trade counts are easily faked; net flow, snipers and fan-out are not.
    # Net OUTflow = distribution: not a runner no matter how the counts look.
    if c.net_flow_usd is not None and c.net_flow_usd <= 0:
        return RunnerScore(c, 0.0, "cold", (),
                           tuple(signals) + ("net outflow (distribution)",))
    if c.net_flow_usd is not None and c.net_flow_usd > 0:
        signals.append(f"net +${c.net_flow_usd:,.0f} in")
    # Launch-block snipers inflate early buys, then dump — discount the score.
    if c.sniper_share is not None:
        score *= 1.0 - policy.sniper_discount * min(max(c.sniper_share, 0.0), 1.0)
        if c.sniper_share >= 0.5:
            signals.append(f"{c.sniper_share:.0%} sniped (discounted)")
    # A deployer fanning tokens to fresh wallets = a farmed book, not real holders.
    if c.transfer_fanout is not None and c.transfer_fanout >= policy.max_fanout:
        score *= 0.4
        signals.append(f"{c.transfer_fanout} fan-out wallets (discounted)")

    score = round(score, 3)
    tier = ("hot" if score >= policy.min_score_hot
            else "watch" if score >= policy.min_score_watch else "cold")
    return RunnerScore(c, score, tier, (), tuple(signals))


def rank_runners(candidates: list[RunnerCandidate], *,
                 policy: RunnerPolicy | None = None,
                 include_cold: bool = False) -> list[RunnerScore]:
    scored = [score_runner(c, policy=policy) for c in candidates]
    keep = {"hot", "watch"} | ({"cold"} if include_cold else set())
    out = [s for s in scored if s.tier in keep]
    out.sort(key=lambda s: s.score, reverse=True)
    return out
