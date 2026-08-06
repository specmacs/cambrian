"""Base LP desk — the equity LP desk, repointed at Base DEX pools.

Same fail-closed engine as desks/lp.py, minus the equity-only gates (earnings
blackout, market-hours windows) because Base is 24/7 crypto. What stays: the
exposure/sizing caps, the venue allowlist, the TVL floor, and the fee-APR-vs-
impermanent-loss stress test.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import base_config
from ..base_config import BaseLPLimits
from ..base.pools import PoolYield
from ..decision import Decision, Gate
from ..units import impermanent_loss, pct, usd

DESK = "base-lp"


@dataclass(frozen=True)
class BaseLPState:
    total_deployed_usd: float
    open_positions: int


def evaluate(
    pool: PoolYield,
    position_usd: float,
    state: BaseLPState,
    *,
    limits: BaseLPLimits | None = None,
    trusted_dexes=None,
) -> Decision:
    limits = limits or base_config.BASE_LP
    trusted_dexes = base_config.TRUSTED_DEXES if trusted_dexes is None else trusted_dexes

    g = Gate(DESK, pool.label or pool.address)
    g.note(f"{pool.dex} {pool.address}")

    g.require(not limits.halted, "Base LP desk is halted")
    g.require(pool.dex in trusted_dexes, f"DEX {pool.dex} not in trusted venues")

    # Sizing / exposure
    g.require(
        position_usd <= limits.max_position_usd,
        f"position {usd(position_usd)} over per-position cap {usd(limits.max_position_usd)}",
    )
    g.require(
        state.total_deployed_usd + position_usd <= limits.max_total_deployed_usd,
        f"would push deployed to {usd(state.total_deployed_usd + position_usd)} "
        f"over cap {usd(limits.max_total_deployed_usd)}",
    )
    g.require(
        state.open_positions < limits.max_positions,
        f"already at {state.open_positions}/{limits.max_positions} positions",
    )

    # Pool depth
    if pool.tvl_usd is None:
        g.reject("pool TVL unknown")
    else:
        g.require(
            pool.tvl_usd >= limits.min_pool_tvl_usd,
            f"TVL {usd(pool.tvl_usd)} below floor {usd(limits.min_pool_tvl_usd)}",
        )

    # Yield: clear the outright floor AND cover stress-move impermanent loss.
    il = impermanent_loss(limits.stress_price_ratio)
    if pool.fee_apr is None:
        g.reject("fee APR unknown")
    else:
        g.note(f"fee APR {pct(pool.fee_apr)} vs stress IL {pct(il)} "
                f"at {limits.stress_price_ratio}x")
        g.require(
            pool.fee_apr >= limits.min_fee_apr,
            f"fee APR {pct(pool.fee_apr)} below floor {pct(limits.min_fee_apr)}",
        )
        g.require(
            pool.fee_apr >= il,
            f"fee APR {pct(pool.fee_apr)} does not cover stress IL {pct(il)}",
        )

    return g.decide()
