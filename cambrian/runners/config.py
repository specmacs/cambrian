"""What the runner tracker watches and how it scores.

LAUNCHPADS is the watchlist of pads to monitor for fresh launches (Pons, Noxa,
Pools.trade, flap.sh, bankr, ArrowPad, Openfair, RobinPad, hood.fun, ...). Fill
each with its factory address and the topic0 of its "token/pool created" event
(from the pad's verified contract on the explorer). Watching a pad is NOT the
same as trusting it — you watch everything to catch runners; `config.TRUSTED_
FACTORIES` is the stricter subset the degen desk will actually clear.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Rough ETH/USD used to value volume + liquidity in dollars. Set RH_WETH_USD to
# roughly the current ETH price (or wire weth_usd_pool to read it on-chain). The
# scorer's thresholds are coarse enough that an approximate price is fine.
WETH_USD = float(os.getenv("RH_WETH_USD", "3000"))

# ~5 minutes of RH blocks (100ms block time -> ~3000 blocks). Tune per real cadence.
WINDOW_BLOCKS = int(os.getenv("RH_WINDOW_BLOCKS", "3000"))

# Where the watch loop remembers already-surfaced tokens (so it alerts once).
SEEN_FILE = os.getenv("RH_SEEN_FILE", "runners_seen.json")

# name -> {address, created_topic0, start_block, amm}
# `created_topic0` is left blank on purpose: discover_fresh SKIPS a pad without
# it (fail closed), so nothing runs on a guessed event signature. Fill it from
# the factory's verified contract on explorer.rhchain.com (the token-created
# event), and CONFIRM the address there before trusting it.
LAUNCHPADS: dict[str, dict[str, str]] = {
    "pons": {
        # UNVERIFIED — confirm on explorer.rhchain.com before going live.
        "address": "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB",
        "created_topic0": "",       # <- fill from the factory's created event
        "start_block": "8991118",
        "amm": "uniswap-v3",        # Pons launches into Uniswap V3 vs WETH
    },
    # Pons legacy factory: 0x0c37a24F5D23A486FA692d1500881d698B1F77a4 (start 8600612)
    # "noxa":  {"address": "0x____", "created_topic0": "", "amm": "uniswap-v3"},
    # (Noxa fee vault 0x9eFdC1A8e6E94f16A228e44f3025E1f346EE0417 is NOT the factory)
}

# RH Chain contract addresses. v3_factory + pool_manager are CHAIN-VERIFIED via
# `runners --find-contracts` (the dominant emitters of PoolCreated / Initialize) —
# RH does NOT use Uniswap's canonical V3 factory address, so don't "fix" it back.
# weth is CHAIN-CONFIRMED too: a live `rh_discover` run read non-zero liquidity on
# many V3 pools paired against it (a wrong WETH would read $0 everywhere). usdg is
# still search-derived — spot-check on robinhoodchain.blockscout.com.
CONTRACTS = {
    "v3_factory": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",   # chain-verified
    "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",  # chain-verified (v4)
    "weth": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",          # chain-confirmed
    "usdg": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
    "native_eth": "0x0000000000000000000000000000000000000000",
    # A deep WETH/USDG V3 pool for pricing WETH in USD (optional; fill from explorer).
    "weth_usd_pool": "",
    # Secondary v3-style factory seen on-chain (2 pools) — likely the Clones fork:
    # 0xe51960f1b45f1c9fb6d166e6a884f866fc70433b
}

# Launchpad fingerprints. A pad is identified two independent ways: the v4 hook
# every launch routes through (authoritative) and a vanity token-address suffix
# (cheap secondary). Both confirmed on-chain for bankr: hook 0x4e34..a544, tokens
# end in 'ba3', launches on Uniswap v4. Add more pads as their fingerprints surface
# (run examples/rh_trace.py on a known token to pull a pad's hook + launch contract).
PAD_BY_HOOK = {
    "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544": "bankr",
}
PAD_BY_SUFFIX = {
    "ba3": "bankr",
}


def pad_of(token: str | None, hook: str | None) -> str | None:
    """Which pad launched this token? Hook match is authoritative; the vanity
    address suffix corroborates (and covers pads/pools with no hook)."""
    h = (hook or "").lower()
    if h in PAD_BY_HOOK:
        return PAD_BY_HOOK[h]
    if token:
        for suf, name in PAD_BY_SUFFIX.items():
            if token.lower().endswith(suf):
                return name
    return None

# Smart-money addresses to follow. A watched wallet buying a fresh token is the
# strongest single signal there is. Curate by hand to start.
WATCHED_WALLETS: set[str] = set()


@dataclass
class RunnerPolicy:
    """Tuned for *fresh* pools, which are thin and young — a lower liquidity floor
    than the degen desk's trade-time gate, because we want to see runners early,
    then let the desk apply its stricter checks before any buy."""
    max_age_minutes: float = 120.0        # older than this isn't "fresh"
    min_liquidity_usd: float = 5_000.0    # see them early; desk re-checks at 50k
    min_holders: int = 15
    max_top_holder_pct: float = 0.25      # rug filter (fraction)
    require_trusted_launchpad: bool = False
    vol_accel_hot: float = 3.0            # 5m volume >= 3x the prior 5m = running
    max_fanout: int = 25                  # wallet-to-wallet transfers above this = farmed
    sniper_discount: float = 0.7          # how hard launch-block sniping cuts the score
    # Tiering.
    min_score_watch: float = 0.30
    min_score_hot: float = 0.60
    # Signal weights (sum ~1.0).
    w_smart_money: float = 0.40
    w_volume: float = 0.25
    w_holders: float = 0.20
    w_buy_skew: float = 0.15


RUNNER = RunnerPolicy()
