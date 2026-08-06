"""Config for the Base pivot — live today on Cambrian's coverage.

The RH/tokenized-equity strategy in config.py is parked (Cambrian doesn't index
Robinhood Chain or Uniswap v4 yet). This is the same LP engine pointed at Base
DEX pools, which Cambrian does index. Kept separate so the parked strategy and
the live one don't get tangled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

BASE_CHAIN_ID = 8453

# Token symbols used to classify a pair's impermanent-loss risk. A stable/stable
# pool barely drifts; a pool with an unknown token is where you get rugged.
STABLES = {"USDC", "USDT", "DAI", "USDBC", "USDS", "GHO", "SDAI", "USD+",
           "EURC", "CRVUSD", "LUSD", "USDE", "USDA"}
BLUECHIPS = {"WETH", "ETH", "CBETH", "WSTETH", "CBBTC", "WBTC", "RETH",
             "EZETH", "WEETH", "AERO"}

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


@dataclass
class BaseLPPolicy:
    """How the allocator turns a deposit into a portfolio.

    The barbell: most capital in low-risk 'core' pools (known tokens, low IL),
    a capped slice in high-yield 'satellite' pools (exotic tokens), the rest held
    as a stable reserve. Aggressive on yield, bounded on how much can blow up.
    """
    # Sleeve budgets as fractions of capital (remainder -> stable reserve).
    core_target: float = 0.60
    satellite_target: float = 0.25
    # Concentration caps.
    max_per_pool: float = 0.20
    max_per_token: float = 0.35
    max_core_positions: int = 4
    max_satellite_positions: int = 3
    # Only count this fraction of emission/incentive APR as durable yield —
    # emissions decay and their tokens dump, so we don't take them at face value.
    emission_credit: float = 0.40
    # Reject anything below these after discounting.
    min_effective_apr: float = 0.03
    min_score: float = 0.0
    min_tvl_usd: float = 250_000.0
    # Rough annualized impermanent-loss cost by pair risk class (tune with data).
    il_stable: float = 0.005
    il_volatile: float = 0.08
    il_exotic: float = 0.20


BASE_POLICY = BaseLPPolicy()


@dataclass
class MonitorPolicy:
    """When to rotate a position, and when to pull the whole book to stables.

    The first three are per-position rotation triggers (the slow-bleed defense).
    The last two are the global circuit breaker: in a market-wide dump you don't
    rotate farm-to-farm — everything's red — you go to stables.
    """
    rotate_apr_frac: float = 0.5    # rotate if a pool's APR falls below 50% of entry
    rotate_tvl_frac: float = 0.5    # rotate if its TVL drains below 50% of entry
    stop_loss_pct: float = 0.25     # rotate if the volatile leg is down 25% from entry
    market_dump_pct: float = 0.15   # benchmark down 15% in the window -> flight to stables
    max_drawdown: float = 0.20      # book down 20% from its high-water mark -> flight to stables


MONITOR = MonitorPolicy()

# Where the paper portfolio's current positions are persisted between runs.
POSITIONS_FILE = os.getenv("BASE_POSITIONS", "positions.json")
