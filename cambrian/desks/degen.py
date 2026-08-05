"""Degen desk: decide whether to snipe a freshly-launched token.

Pure policy. Given a `DegenCandidate` (the facts), the desk's current `DegenState`
(what we already hold / have done), and the limits, it returns a `Decision`.
No chain, no clock, no side effects — so every gate below is directly testable.

The gates are a faithful transcription of `DegenLimits`. If you loosen a limit,
loosen it in config; do not special-case it here.
"""

from __future__ import annotations

from .. import config
from ..config import DegenLimits
from ..decision import Decision, Gate
from ..snapshots import DegenCandidate, DegenState
from ..units import addr_in, pct, usd

DESK = "degen"


def evaluate(
    candidate: DegenCandidate,
    state: DegenState,
    *,
    limits: DegenLimits | None = None,
    trusted_factories=None,
    reviewed_hooks=None,
) -> Decision:
    """Approve only if every limit clears. Allowlists and limits default to the
    live config but are injectable so tests (and dry-run scenarios) can supply
    their own without mutating global state."""
    limits = limits or config.DEGEN
    trusted_factories = config.TRUSTED_FACTORIES if trusted_factories is None else trusted_factories
    reviewed_hooks = config.REVIEWED_HOOKS if reviewed_hooks is None else reviewed_hooks

    g = Gate(DESK, candidate.token)
    if candidate.symbol:
        g.note(f"claimed symbol {candidate.symbol!r} (untrusted)")

    # --- Kill switch --------------------------------------------------------
    g.require(not limits.halted, "degen desk is halted")

    # --- Sizing / exposure caps --------------------------------------------
    g.require(
        candidate.notional_usd <= limits.max_notional_usd,
        f"notional {usd(candidate.notional_usd)} exceeds per-trade cap "
        f"{usd(limits.max_notional_usd)}",
    )
    g.require(
        state.daily_notional_usd + candidate.notional_usd <= limits.max_daily_notional_usd,
        f"would push daily notional to "
        f"{usd(state.daily_notional_usd + candidate.notional_usd)} over cap "
        f"{usd(limits.max_daily_notional_usd)}",
    )
    g.require(
        state.open_positions < limits.max_open_positions,
        f"already at {state.open_positions}/{limits.max_open_positions} open positions",
    )
    g.require(
        state.trades_last_hour < limits.max_trades_per_hour,
        f"already {state.trades_last_hour}/{limits.max_trades_per_hour} trades this hour",
    )

    # --- Pool / token quality ----------------------------------------------
    if candidate.pool_liquidity_usd is None:
        g.reject("pool liquidity unknown")
    else:
        g.require(
            candidate.pool_liquidity_usd >= limits.min_pool_liquidity_usd,
            f"pool liquidity {usd(candidate.pool_liquidity_usd)} below floor "
            f"{usd(limits.min_pool_liquidity_usd)}",
        )

    if candidate.expected_slippage_bps is None:
        g.reject("expected slippage unknown")
    else:
        g.require(
            candidate.expected_slippage_bps <= limits.max_slippage_bps,
            f"expected slippage {candidate.expected_slippage_bps:.0f} bps over cap "
            f"{limits.max_slippage_bps} bps",
        )

    if candidate.top_holder_pct is None:
        g.reject("top-holder concentration unknown")
    else:
        g.require(
            candidate.top_holder_pct <= limits.max_top_holder_pct / 100.0,
            f"top holder holds {pct(candidate.top_holder_pct)}, over cap "
            f"{limits.max_top_holder_pct:.0f}%",
        )

    # --- Provenance allowlists ---------------------------------------------
    if limits.require_trusted_factory:
        g.require(
            addr_in(candidate.factory, trusted_factories),
            f"factory {candidate.factory or '<none>'} not in trusted allowlist",
        )

    if limits.require_hook_review:
        # No hook at all is fine. A hook that exists must have been reviewed.
        if candidate.hook is not None:
            g.require(
                addr_in(candidate.hook, reviewed_hooks),
                f"pool hook {candidate.hook} has not been source-reviewed",
            )

    return g.decide()
