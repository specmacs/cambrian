"""
Single source of truth for everything the desks need to know.

Every address below is a placeholder. The system FAILS CLOSED on unconfigured
values - it will refuse to trade rather than trade blind. That is intentional;
fill these in from the block explorer, do not guess.
"""

import os
from dataclasses import dataclass, field

# Load .env if python-dotenv is installed. Without this, .env is a decorative
# file and every getenv below silently returns "" - which fails closed, but
# confusingly.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    if os.path.exists(".env"):
        print("\033[33mWarning: .env exists but python-dotenv is not installed. "
              "Run `pip install python-dotenv` or export the vars manually.\033[0m")

# --- Chain -------------------------------------------------------------------

CHAIN_ID = 4663                       # Robinhood Chain mainnet
CHAIN_NAME = "Robinhood Chain"
RPC_URL = os.getenv("RH_RPC_URL", "")  # from Chainstack / 1inch / Goldsky / public
EXPLORER = "https://explorer.rhchain.com"  # verify; Blockscout instance
NATIVE_GAS_TOKEN = "ETH"
BLOCK_TIME_MS = 100

# Robinhood Chain uses first-come-first-served sequencing: a higher priority
# fee does NOT jump the queue. Latency matters, gas bidding does not.
FCFS_SEQUENCING = True


# --- Capital segregation -----------------------------------------------------
#
# Two wallets. No code path moves funds between them. If the degen desk blows
# up, LP capital is untouched, because the degen desk has never held its key.

DEGEN_WALLET = os.getenv("DEGEN_WALLET", "")
LP_WALLET = os.getenv("LP_WALLET", "")


# --- Contract allowlists -----------------------------------------------------

# Token factories / launchpads whose minted tokens use known-standard bytecode.
# Match on ADDRESS ONLY. Names are trivially impersonated.
TRUSTED_FACTORIES: dict[str, str] = {
    # "0x____": "Pons launchpad v2",
}

# Uniswap deployments on Robinhood Chain. Populate from
# developers.uniswap.org/docs/protocols/v3/deployments (and the v4 equivalent).
UNISWAP = {
    "v3_factory": "",
    "v4_pool_manager": "",
    "universal_router": "",
    "quoter": "",
}

# v4 hooks whose source you have personally read. Empty is the correct start.
# Being in Uniswap's public hooklist registry is necessary, not sufficient.
REVIEWED_HOOKS: set[str] = set()

# Tokenized equity tokens eligible for LP, mapped to their underlying ticker.
# The ticker drives the earnings calendar lookup.
STOCK_TOKENS: dict[str, str] = {
    # "0x____": "NVDA",
}

# Stable/quote assets.
QUOTE_TOKENS: dict[str, str] = {
    # "0x____": "USDG",
}


# --- Desk limits -------------------------------------------------------------

@dataclass
class DegenLimits:
    """Deliberately punishing. Most of these positions go to zero; the limits
    are what make that survivable rather than terminal."""
    max_notional_usd: float = 25.0
    max_daily_notional_usd: float = 100.0
    max_open_positions: int = 4
    max_trades_per_hour: int = 3
    min_pool_liquidity_usd: float = 50_000.0
    max_slippage_bps: int = 300
    max_top_holder_pct: float = 20.0
    require_trusted_factory: bool = True
    require_hook_review: bool = True
    halted: bool = False


@dataclass
class LPLimits:
    max_position_usd: float = 500.0
    max_total_deployed_usd: float = 2_000.0
    min_pool_tvl_usd: float = 1_000_000.0
    max_positions: int = 3
    # Exit this many minutes before the underlying equity market closes.
    exit_before_close_minutes: int = 20
    # Wait this long after the open before redeploying, to let the gap resolve.
    reenter_after_open_minutes: int = 15
    # Stay fully out of a name from this many days before its earnings.
    earnings_blackout_days: int = 1
    # Reject a pool whose fee APR does not survive this price move.
    stress_price_ratio: float = 1.25
    halted: bool = False


DEGEN = DegenLimits()
LP = LPLimits()

# Global. Set true only when you have watched the journal for weeks.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

ORDER_JOURNAL = os.getenv("ORDER_JOURNAL", "journal.jsonl")


# --- Config validation -------------------------------------------------------

def missing_config() -> list[str]:
    """What is unset. Anything listed here makes the relevant desk fail closed."""
    gaps = []
    if not RPC_URL:
        gaps.append("RH_RPC_URL (no chain access)")
    if not DEGEN_WALLET:
        gaps.append("DEGEN_WALLET")
    if not LP_WALLET:
        gaps.append("LP_WALLET")
    if DEGEN_WALLET and LP_WALLET and DEGEN_WALLET.lower() == LP_WALLET.lower():
        gaps.append("DEGEN_WALLET == LP_WALLET (capital segregation defeated)")
    if not TRUSTED_FACTORIES:
        gaps.append("TRUSTED_FACTORIES (degen desk cannot clear any token)")
    if not UNISWAP["v4_pool_manager"]:
        gaps.append("UNISWAP.v4_pool_manager")
    if not STOCK_TOKENS:
        gaps.append("STOCK_TOKENS (LP desk has nothing to provide against)")
    return gaps
