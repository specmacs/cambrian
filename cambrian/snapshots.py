"""Plain-data views of on-chain / market facts.

The desks never touch the chain directly. They consume these frozen snapshots
and emit a `Decision`. That separation is what lets the entire risk engine be
unit-tested with fixtures and no network — the interesting, dangerous logic is
pure. Fetching the facts (RPC, explorer, subgraph, earnings feed) is a separate,
best-effort concern in `cambrian.chain` and `cambrian.feeds`.

A field set to `None` means "we could not determine this". The desks treat an
unknown value as a rejection, not a pass: fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# --- Degen desk -------------------------------------------------------------

@dataclass(frozen=True)
class DegenCandidate:
    """A freshly-launched token the degen desk is considering sniping."""
    token: str
    #: Factory/launchpad that minted the token, if identifiable.
    factory: str | None
    #: v4 hook governing the pool. None means "no hook" (a bare pool). A hook
    #: we can't identify should be passed as its address so the review gate can
    #: reject it — do not pass None to paper over an unknown hook.
    hook: str | None
    #: Quote-side liquidity in the pool, in USD.
    pool_liquidity_usd: float | None
    #: Largest single holder's share of supply, as a fraction (0.20 == 20%).
    top_holder_pct: float | None
    #: Expected slippage in bps for the intended trade size at current depth.
    expected_slippage_bps: float | None
    #: The USD size we intend to buy.
    notional_usd: float
    #: Claimed symbol, for the journal only. Never trusted for decisions.
    symbol: str | None = None


@dataclass(frozen=True)
class DegenState:
    """The desk's current standing, needed to enforce the rate/exposure caps.
    Derived from the journal (or an in-memory ledger) at decision time."""
    open_positions: int
    trades_last_hour: int
    daily_notional_usd: float


# --- LP desk ----------------------------------------------------------------

@dataclass(frozen=True)
class LPPool:
    """A tokenized-equity / quote pool the LP desk is considering."""
    pool: str
    #: The tokenized-equity token address (must be in STOCK_TOKENS).
    stock_token: str
    #: The quote/stable token address (must be in QUOTE_TOKENS).
    quote_token: str
    #: Total value locked, in USD.
    tvl_usd: float | None
    #: Current annualized fee yield as a fraction (0.30 == 30% APR).
    fee_apr: float | None
    #: The USD size we intend to deploy.
    position_usd: float


@dataclass(frozen=True)
class LPContext:
    """Everything time/calendar dependent for a given underlying, resolved at
    decision time. `minutes_*` are None when the market is closed."""
    now_utc: datetime
    ticker: str
    market_open: bool
    minutes_since_open: float | None
    minutes_to_close: float | None
    #: Days until the next earnings report. None means "unknown" -> blackout,
    #: because trading a name blind over earnings is exactly the risk the
    #: blackout exists to remove.
    days_to_earnings: float | None


@dataclass(frozen=True)
class LPState:
    total_deployed_usd: float
    open_positions: int
