"""What the runner tracker watches and how it scores.

LAUNCHPADS is the watchlist of pads to monitor for fresh launches (Pons, Noxa,
Pools.trade, flap.sh, bankr, ArrowPad, Openfair, RobinPad, hood.fun, ...). Fill
each with its factory address and the topic0 of its "token/pool created" event
(from the pad's verified contract on the explorer). Watching a pad is NOT the
same as trusting it — you watch everything to catch runners; `config.TRUSTED_
FACTORIES` is the stricter subset the degen desk will actually clear.
"""

from __future__ import annotations

from dataclasses import dataclass

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

# RH Chain contract addresses — search-derived, ALL UNVERIFIED. Confirm every
# one on explorer.rhchain.com before going live. Discovery watches the V3
# factory's PoolCreated event, which catches every fresh WETH pool regardless of
# which pad minted the token — so these few addresses replace per-pad event ABIs.
CONTRACTS = {
    # Uniswap's canonical deterministic V3 factory (same across most chains).
    "v3_factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    "weth": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
    "usdg": "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
    # A deep WETH/USDG V3 pool for pricing WETH in USD (fill from the explorer).
    "weth_usd_pool": "",
}

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
    # Tiering.
    min_score_watch: float = 0.30
    min_score_hot: float = 0.60
    # Signal weights (sum ~1.0).
    w_smart_money: float = 0.40
    w_volume: float = 0.25
    w_holders: float = 0.20
    w_buy_skew: float = 0.15


RUNNER = RunnerPolicy()
