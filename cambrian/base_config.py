"""Config for the Base pivot — live today on Cambrian's coverage.

The RH/tokenized-equity strategy in config.py is parked (Cambrian doesn't index
Robinhood Chain or Uniswap v4 yet). This is the same LP engine pointed at Base
DEX pools, which Cambrian does index. Kept separate so the parked strategy and
the live one don't get tangled.
"""

from __future__ import annotations

from dataclasses import dataclass

BASE_CHAIN_ID = 8453

# DEX -> the Cambrian "list pools" endpoint for it (confirmed present in the API
# path list). Aerodrome v3 only exposed /pool (single), so it's omitted here.
BASE_POOL_ENDPOINTS: dict[str, str] = {
    "aerodrome-v2": "/evm/aero/v2/pools",
    "uniswap-v3": "/evm/uniswap/v3/pools",
    "pancake-v3": "/evm/pancake/v3/pools",
    "sushi-v3": "/evm/sushi/v3/pools",
    "alienbase-v3": "/evm/alien/v3/pools",
    "clones-v3": "/evm/clones/v3/pools",
}

# The desk only touches DEXes we've chosen to trust. Same address-allowlist
# discipline as the degen desk's factories, at the venue level.
TRUSTED_DEXES: set[str] = set(BASE_POOL_ENDPOINTS)


@dataclass
class BaseLPLimits:
    """Base LP is 24/7 crypto — no earnings blackout, no market-hours windows.
    The stress ratio is wider than the equity desk's because crypto moves more."""
    max_position_usd: float = 500.0
    max_total_deployed_usd: float = 2_000.0
    max_positions: int = 5
    min_pool_tvl_usd: float = 250_000.0
    # Reject a pool whose fee APR doesn't clear this floor outright...
    min_fee_apr: float = 0.05
    # ...and doesn't survive the impermanent loss of this price move.
    stress_price_ratio: float = 1.5
    halted: bool = False


BASE_LP = BaseLPLimits()
