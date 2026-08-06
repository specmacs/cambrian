"""Risk-adjusted scoring — turns a raw pool into "how good is this, really".

The scanner ranks by headline APR. That's naive: a 200% pool that's 95%
emissions in an unknown token, paired volatile, is a trap. This scorer:

  * discounts emissions (they decay/dump — only a fraction counts as durable),
  * subtracts an impermanent-loss cost sized to the pair's volatility class,
  * hard-rejects thin or yieldless pools,

and sorts each survivor into a sleeve: `core` (known tokens, lower IL — the
anchor) or `satellite` (exotic, higher yield, capped exposure).

The IL costs and emission discount are deliberately rough, defensible
placeholders — tune them once you have realized fee/IL data in the journal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import base_config
from ..base_config import BaseLPPolicy
from ..base.pools import PoolYield


@dataclass(frozen=True)
class PoolScore:
    pool: PoolYield
    pair_class: str      # "stable" | "volatile" | "exotic"
    risk: str            # "low" | "med" | "high"
    effective_apr: float  # emission-discounted yield, before IL
    il_penalty: float
    score: float          # effective_apr - il_penalty  (risk-adjusted net APR)
    sleeve: str           # "core" | "satellite" | "reject"
    reasons: tuple[str, ...]


def _classify(label: str, policy: BaseLPPolicy) -> tuple[str, str, float]:
    """(pair_class, risk, il_penalty) from the pool's SYM0/SYM1 label."""
    if "/" not in label:
        return "exotic", "high", policy.il_exotic
    s0, s1 = (s.strip().upper() for s in label.split("/", 1))
    stab, blue = base_config.STABLES, base_config.BLUECHIPS
    known = stab | blue
    if s0 in stab and s1 in stab:
        return "stable", "low", policy.il_stable
    if s0 in known and s1 in known:
        return "volatile", "med", policy.il_volatile
    return "exotic", "high", policy.il_exotic


def score_pool(pool: PoolYield, policy: BaseLPPolicy | None = None) -> PoolScore:
    policy = policy or base_config.BASE_POLICY
    reasons: list[str] = []
    pair_class, risk, il = _classify(pool.label, policy)

    # Hard rejects first (fail closed).
    if pool.tvl_usd is None or pool.tvl_usd < policy.min_tvl_usd:
        return PoolScore(pool, pair_class, risk, 0.0, il, 0.0, "reject",
                         ("TVL below floor / unknown",))
    if pool.fee_apr is None:
        return PoolScore(pool, pair_class, risk, 0.0, il, 0.0, "reject",
                         ("no yield data",))

    swap = pool.swap_fee_apr if pool.swap_fee_apr is not None else 0.0
    emissions = max(pool.fee_apr - swap, 0.0)
    effective = swap + policy.emission_credit * emissions
    score = effective - il

    if emissions > 0:
        share = emissions / pool.fee_apr if pool.fee_apr else 0
        reasons.append(f"{share:.0%} of APR is emissions (counted at "
                       f"{policy.emission_credit:.0%})")
    reasons.append(f"{pair_class} pair, IL cost ~{il:.1%}")

    if effective < policy.min_effective_apr:
        return PoolScore(pool, pair_class, risk, effective, il, score, "reject",
                         tuple(reasons + [f"effective APR {effective:.1%} below floor"]))
    if score < policy.min_score:
        return PoolScore(pool, pair_class, risk, effective, il, score, "reject",
                         tuple(reasons + [f"net-of-IL score {score:.1%} not positive"]))

    sleeve = "core" if risk in ("low", "med") else "satellite"
    return PoolScore(pool, pair_class, risk, effective, il, score, sleeve,
                     tuple(reasons))


def score_all(pools: list[PoolYield], policy: BaseLPPolicy | None = None) -> list[PoolScore]:
    return [score_pool(p, policy) for p in pools]
