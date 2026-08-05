"""LP desk: decide whether to open a liquidity position in a tokenized-equity pool.

Pure policy, same shape as the degen desk. The extra complexity here is that the
decision is time- and calendar-aware: a pool that is fine at 11:00 is off-limits
15 minutes before the close, the day before earnings, or before the morning gap
has had time to resolve. All of that arrives pre-computed on `LPContext`; this
module only compares.

A new position must clear *every* window. Being late in the day, inside the
earnings blackout, or too soon after the open each independently blocks a
*new* deploy. (Exiting an existing position is a separate action and is not
gated here — you always want to be able to reduce risk.)
"""

from __future__ import annotations

from .. import config
from ..config import LPLimits
from ..decision import Decision, Gate
from ..snapshots import LPContext, LPPool, LPState
from ..units import addr_in, impermanent_loss, pct, usd

DESK = "lp"


def evaluate(
    pool: LPPool,
    ctx: LPContext,
    state: LPState,
    *,
    limits: LPLimits | None = None,
    stock_tokens=None,
    quote_tokens=None,
) -> Decision:
    limits = limits or config.LP
    stock_tokens = config.STOCK_TOKENS if stock_tokens is None else stock_tokens
    quote_tokens = config.QUOTE_TOKENS if quote_tokens is None else quote_tokens

    g = Gate(DESK, pool.pool)
    g.note(f"underlying {ctx.ticker}")

    # --- Kill switch --------------------------------------------------------
    g.require(not limits.halted, "LP desk is halted")

    # --- Eligible assets ----------------------------------------------------
    g.require(
        addr_in(pool.stock_token, stock_tokens),
        f"stock token {pool.stock_token} not in STOCK_TOKENS allowlist",
    )
    g.require(
        addr_in(pool.quote_token, quote_tokens),
        f"quote token {pool.quote_token} not in QUOTE_TOKENS allowlist",
    )

    # --- Sizing / exposure caps --------------------------------------------
    g.require(
        pool.position_usd <= limits.max_position_usd,
        f"position {usd(pool.position_usd)} exceeds per-position cap "
        f"{usd(limits.max_position_usd)}",
    )
    g.require(
        state.total_deployed_usd + pool.position_usd <= limits.max_total_deployed_usd,
        f"would push deployed capital to "
        f"{usd(state.total_deployed_usd + pool.position_usd)} over cap "
        f"{usd(limits.max_total_deployed_usd)}",
    )
    g.require(
        state.open_positions < limits.max_positions,
        f"already at {state.open_positions}/{limits.max_positions} positions",
    )

    # --- Pool depth ---------------------------------------------------------
    if pool.tvl_usd is None:
        g.reject("pool TVL unknown")
    else:
        g.require(
            pool.tvl_usd >= limits.min_pool_tvl_usd,
            f"pool TVL {usd(pool.tvl_usd)} below floor {usd(limits.min_pool_tvl_usd)}",
        )

    # --- Market-hours windows (new deploys only) ---------------------------
    g.require(ctx.market_open, "underlying market is closed")
    if ctx.market_open:
        if ctx.minutes_since_open is None:
            g.reject("minutes-since-open unknown while market open")
        else:
            g.require(
                ctx.minutes_since_open >= limits.reenter_after_open_minutes,
                f"only {ctx.minutes_since_open:.0f} min since open, need "
                f"{limits.reenter_after_open_minutes} for the gap to resolve",
            )
        if ctx.minutes_to_close is None:
            g.reject("minutes-to-close unknown while market open")
        else:
            g.require(
                ctx.minutes_to_close > limits.exit_before_close_minutes,
                f"only {ctx.minutes_to_close:.0f} min to close, inside the "
                f"{limits.exit_before_close_minutes}-min exit window",
            )

    # --- Earnings blackout --------------------------------------------------
    if ctx.days_to_earnings is None:
        g.reject(f"earnings date for {ctx.ticker} unknown (blackout by default)")
    else:
        g.require(
            ctx.days_to_earnings > limits.earnings_blackout_days,
            f"{ctx.days_to_earnings:.1f} days to earnings, inside "
            f"{limits.earnings_blackout_days}-day blackout",
        )

    # --- Fee-APR stress test ------------------------------------------------
    # Reject a pool whose fee yield doesn't at least cover the impermanent loss
    # a stress-sized price move would inflict. This is a floor, not a model:
    # `impermanent_loss` is the full-range approximation and understates the
    # loss for a concentrated position, so clearing it is necessary, not
    # sufficient. Calibrate `stress_price_ratio` (and consider a margin above
    # IL) once you have real fee data in the journal.
    il = impermanent_loss(limits.stress_price_ratio)
    if pool.fee_apr is None:
        g.reject("fee APR unknown")
    else:
        g.note(f"fee APR {pct(pool.fee_apr)} vs stress IL {pct(il)} "
                f"at {limits.stress_price_ratio}x")
        g.require(
            pool.fee_apr >= il,
            f"fee APR {pct(pool.fee_apr)} does not cover stress IL {pct(il)} "
            f"at a {limits.stress_price_ratio}x move",
        )

    return g.decide()
