"""
Single source of truth for everything the desks need to know.

Every address below is a placeholder. The system FAILS CLOSED on unconfigured
values - it will refuse to trade rather than trade blind. That is intentional;
fill these in from the block explorer, do not guess.
"""

import os
from dataclasses import dataclass, field

# Credentials with a distinctive, unambiguous prefix. Anything listed here can be
# recovered from a mangled file by pattern rather than by position -- see the
# salvage pass in `_load_key_file`.
CREDENTIAL_PREFIXES = {
    "DEFINITIVE_API_KEY": "dpka_",
    "DEFINITIVE_API_SECRET": "dpks_",
}

# name -> where the value came from. Diagnostics only; never holds a value.
KEY_SOURCES: dict[str, str] = {}


def key_file_candidates() -> list:
    """The files `_load_key_file` reads, in order, deduplicated.

    Shared with the `keys` command so the diagnostic can never disagree with the
    loader about where it looked -- and so a home directory that *is* the working
    directory gets listed once rather than twice.
    """
    import pathlib as _p
    out: list = []
    for candidate in (_p.Path.cwd() / "cambrian.env", _p.Path.home() / "cambrian.env"):
        try:
            resolved = str(candidate.resolve())
        except Exception:
            resolved = str(candidate)
        if not any(resolved == seen for _, seen in out):
            out.append((candidate, resolved))
    return [c for c, _ in out]


def credentials_in_files() -> dict:
    """{name: path} for each prefixed credential visible in a key file.

    Reports WHERE one was seen, never what it is. Lets a diagnostic distinguish
    "the file is wrong" from "the file is fine and something else overrode it",
    which is the distinction that actually tells someone what to go fix.
    """
    import re
    out: dict = {}
    for candidate in key_file_candidates():
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf8", errors="replace")
        except Exception:
            continue
        for name, prefix in CREDENTIAL_PREFIXES.items():
            if name not in out and re.search(
                    re.escape(prefix) + r"[A-Za-z0-9_\-]{8,}", text):
                out[name] = str(candidate)
    return out


def _load_key_file() -> None:
    """Read `cambrian.env` from the working directory or the user's home.

    Exists because setting secrets from a shell is where non-developers actually
    get stuck: PowerShell mangles unquoted values with special characters, quoted
    values end up with the quotes baked in, and an interactive prompt invites
    pasting the next command as the value. A plain KEY=VALUE file has none of
    those semantics -- a text editor just holds text.

    Never overrides an environment variable that is already set, so a real
    environment still wins over a convenience file.

    **Then a salvage pass**, because KEY=VALUE still assumes the person editing
    the file put the right thing on the right line, and in practice they paste a
    whole terminal line -- prompt, command and all -- or swap the two values.
    Both are recoverable without guessing: `dpka_`/`dpks_` are unambiguous, so a
    credential that is missing or carries the wrong prefix is re-found by
    scanning the file text for its prefix. Position stops mattering; only the
    credential itself does.
    """
    from_file: set = set()
    for candidate in key_file_candidates():
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf8", errors="replace")
        except Exception:
            continue        # a malformed file must never stop the desk booting
        for raw in text.splitlines():
            # Per LINE, not per file: one unusable line (a NUL byte, a name the
            # OS environment will not accept) must not discard the good lines
            # below it, and must not skip the salvage pass either.
            try:
                line = raw.strip().lstrip("﻿")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name = name.strip()
                # Tolerate quotes and stray whitespace: someone pasting a secret
                # into Notepad should not have to know shell quoting rules.
                value = value.strip().strip('"').strip("'").strip()
                if name and value and not os.getenv(name):
                    os.environ[name] = value
                    from_file.add(name)
                    KEY_SOURCES[name] = str(candidate)
            except Exception:
                continue
        try:
            _salvage(text, str(candidate), from_file)
        except Exception:
            pass


def _salvage(text: str, source: str, from_file: set) -> None:
    """Re-find prefixed credentials anywhere in the file's text.

    A correctly-prefixed environment variable is authoritative and is never
    touched. But a value that does NOT carry the prefix cannot be this
    credential at all, and one turned up in the wild: a shell variable set to a
    copied terminal prompt, which then silently outranked a perfectly good file
    because "the environment wins" was applied to a value that was not a
    credential in the first place. So the file wins over shell text — loudly,
    with the override recorded in KEY_SOURCES so `keys` can say it happened
    rather than quietly doing the right thing for an unexplained reason.
    """
    import re
    for name, prefix in CREDENTIAL_PREFIXES.items():
        current = os.getenv(name)
        if current and current.startswith(prefix):
            continue
        m = re.search(re.escape(prefix) + r"[A-Za-z0-9_\-]{8,}", text)
        if not m:
            continue
        overrode_shell = bool(current) and name not in from_file
        os.environ[name] = m.group(0)
        from_file.add(name)
        KEY_SOURCES[name] = (
            "%s — OVERRODE a shell value that is not a %s credential"
            % (source, prefix) if overrode_shell
            else "%s (recovered from the line's text)" % source)


_load_key_file()

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
# Defaulted, not left empty. Failing closed on an unset RPC made sense while the
# chain was unknown; it is now a public, constant, documented endpoint, and the
# only thing the empty default actually produced was "RPC_URL is not set" every
# time a new shell window forgot an environment variable. Override still works.
DEFAULT_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
RPC_URL = os.getenv("RH_RPC_URL") or DEFAULT_RPC_URL
EXPLORER = "https://robinhoodchain.blockscout.com"  # official Blockscout instance
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
