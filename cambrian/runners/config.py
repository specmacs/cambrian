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

# --- Independent-pad launch events (NOT via Uniswap's Liquidity Launcher) -----
# Pons and flap run their own factories, so they never appear in TokenCreated /
# TokenDistributed. These topic0s were recovered from the chain and then pinned
# by keccak256 of the recovered signature, so they are proven, not inferred.
#
# CAUTION — this corrects an earlier conclusion. A previous session brute-forced
# 111,110 candidate signatures against FRONG's launch tx, found no `TokenLaunched`,
# and recorded "there is no TokenLaunched event; do not go hunting for it again".
# That was right about *Uniswap's launcher* and wrong as a general claim: Pons's
# own factory emits a literal `TokenLaunched`. The sniper contact's phrase was
# accurate — it just belongs to a pad we had not yet identified.
#
# Pons emits BOTH per launch, in the same tx (105 and 106 firings over the same
# 40k-block window — one each per token):
#   TokenDeployed(address indexed token, address indexed pool,
#                 address indexed v3Factory, address quote, uint256, uint256)
#   TokenLaunched(address indexed token, address indexed pool,
#                 address indexed v3Factory, address quote, address creator,
#                 uint256, uint256, uint256, uint256, uint256)
# topic1 is the new token, topic2 its v3 pool, topic3 the v3 factory (constant),
# and the first data word is WETH. On TokenLaunched the last data word is the
# creator's initial buy in wei (observed 0, 0.2e18, 3.5e18) — real entry signal.
EVT_PONS_TOKEN_DEPLOYED = "0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a"
EVT_PONS_TOKEN_LAUNCHED = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"

# flap's launch event carries the metadata inline, so name/symbol need no extra
# eth_call: (uint256 timestamp, address creator, uint256 launchId, address token,
# ...offsets..., string name, string symbol, string ipfsCid). It is unindexed, so
# it is matched on emitter+topic0 only — still unforgeable, since only the pad can
# emit at the pad's address. ~403 firings / 40k blocks. The signature text is not
# recovered yet, so this topic0 is chain-observed rather than keccak-proven.
EVT_FLAP_LAUNCH = "0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603"
# Companion, same count, same tx: (address token, uint256, uint256, uint256).
EVT_FLAP_LIQUIDITY = "0x71a10912a55f73d3cced0d1515c2b33c396c80342522bad0e295ccbede556f37"

# name -> {address, created_topic0, start_block, amm}
# `created_topic0` is left blank on purpose: discover_fresh SKIPS a pad without
# it (fail closed), so nothing runs on a guessed event signature. Fill it from
# the factory's verified contract on explorer.rhchain.com (the token-created
# event), and CONFIRM the address there before trusting it.
LAUNCHPADS: dict[str, dict[str, str]] = {
    "pons": {
        # CHAIN-VERIFIED. Blockscout reports this contract as `PonsLaunchFactory`
        # (source-verified), and it is the creator of the `PonsLauncherToken`
        # contracts. `created_topic0` is TokenDeployed — see EVT_PONS_* below.
        "address": "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB",
        "created_topic0": EVT_PONS_TOKEN_DEPLOYED,
        "start_block": "8991118",
        "amm": "uniswap-v3",        # Pons launches into Uniswap V3 vs WETH
    },
    "flap": {
        # CHAIN-VERIFIED as the factory behind the '7777' tokens. It is a
        # TransparentUpgradeableProxy whose implementation (0x7bc20c2c..fa06) is
        # named `Portal`; the "flap" label is inherited from PAD_BY_SUFFIX below,
        # which already had flap='7777' confirmed. What is verified on-chain is
        # the address + topic0 pair, not the brand name — every ...7777 token
        # sampled (Prism Assets, flapons, CRASHCAT, Dank) was deployed by it.
        "address": "0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09",
        "created_topic0": EVT_FLAP_LAUNCH,
        "start_block": "27174072",  # block of the earliest sampled deployment
        "amm": "uniswap-v3",
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

# Launchpad fingerprints. A pad is identified up to three independent ways: the v4
# hook every launch routes through (authoritative), the launch-tx deployer/factory
# (for hookless pads — needs a tx lookup, see rh_trace/rh_pads), and a vanity
# token-address suffix (cheap). Confirmed for bankr: hook 0x4e34..a544, tokens end
# in 'ba3', v4. Name more pads as the census (examples/rh_pads.py) surfaces them —
# either edit these maps or drop them in RH_PADS_FILE (merged at import, no code edit).
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

PAD_BY_HOOK: dict[str, str] = {
    "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544": "bankr",
    # 0x75a54357.. is a distinct unnamed HOOKED pad (~23 launches) — NOT flap (flap
    # is hookless, IDed by the 7777 suffix). Name it once traced.
}
PAD_BY_DEPLOYER: dict[str, str] = {
    # 0x0000ffff.. is DELIBERATELY ABSENT. We used to label it "pons" because it
    # was ~54% of sampled deployers, but Uniswap's own liquidity-launcher repo
    # publishes it as Liquidity Launcher core v3.2.0 on Robinhood Chain — shared
    # infrastructure that EVERY pad routes through, not a pad. Labelling it "pons"
    # stamped a trusted name on launches from any pad, including impostors, which
    # is how unverified contracts got through the gate. Pads are told apart by the
    # `strategy` in TokenDistributed instead; see LAUNCHER/STRATEGY below.
    # 0x58daec.. was also removed. We had it as "pools-trade" on the theory that it
    # deployed FRONG's pools. Reading FRONG's actual launch tx on-chain, that tx's
    # `to` is the v3.0.0 launcher, and 0x58daec.. merely emits an ERC-20 Transfer at
    # its own address inside that tx — it is a token in the launch, not the pad.
    # Pools.trade is now identified by its strategy instead (see PAD_BY_STRATEGY).
}
PAD_BY_SUFFIX: dict[str, str] = {
    "ba3": "bankr",
    "777": "flap",          # confirmed: Flap grinds '7777' token addresses (hookless)
}
# Suffixes stay a last-resort HINT and must never gate a buy — anyone can grind a
# vanity address. flap now has an unforgeable emitter+topic0 in LAUNCHPADS, which
# supersedes this map for identification. Case in point: 0x20024E..7777 was long
# recorded as the flagship "Flap" token; reading name()/symbol() on-chain, it is
# actually 'Prism Assets'/'PRISM'. It IS a flap-factory launch, but the ticker was
# wrong — which is exactly the failure mode suffix-matching invites.

# --- Verified launch path -------------------------------------------------
# Addresses and event signatures below come from Uniswap's published repos
# (liquidity-launcher, continuous-clearing-auction), not from inference. The
# topic0s are keccak256 of the declared signatures.
#
# Only the launcher can emit logs at the launcher's address, so a token that
# appears in TokenCreated came from the real launch path BY CONSTRUCTION — an
# impostor cannot forge membership the way it can forge an address suffix.
# TWO launcher deployments are live on RH and BOTH still emit. Watching only the
# newer one silently misses launches — FRONG (the confirmed pools.trade flagship)
# went through v3.0.0, which is why an earlier scan reported it as "not launched
# via the launcher" at all. Always iterate the whole dict.
UNI_LAUNCHERS = {
    "v3.2.0": "0x0000fffFbe8efe702c8703ae3477ff5de3d319c0",   # 330 dists / 40k blocks
    "v3.0.0": "0x00004c4ccc709ef590f7c81102c0689f0263d4e9",   # 7 dists / 40k blocks
}
UNI_LAUNCHER = UNI_LAUNCHERS["v3.2.0"]                        # back-compat alias

EVT_TOKEN_CREATED = "0x2e2b3f61b70d2d131b2a807371103cc98d51adcaa5e9a8f9c32658ad8426e74e"
EVT_TOKEN_DISTRIBUTED = "0x67226bacccef969dab310a9e55dc1cf821363658e433fd330344f5cc00c79ac8"
EVT_AUCTION_CREATED = "0x7ede475fad18ccf0039f2b956c4d43a8b4ed0853de4daaa8ae25299f331ae3b9"

# TokenDistributed(token, strategy, amount): the strategy is the pad's real
# identity — each pad runs its own strategy/fee-splitter pair. Names are the
# contract roles from the repo; which pad operates which is still being mapped
# by examples/rh_launcher.py, so these stay descriptive rather than branded.
PAD_BY_STRATEGY: dict[str, str] = {
    # CONFIRMED: FRONG (0x6245e6..0c47), the pools.trade flagship, was distributed
    # through this strategy on the v3.0.0 launcher. Chain-verified, not inferred.
    "0x60d73b21cdf2ea846ab3d58699bbbb8f29d72491": "pools-trade",
    # Named from the roles Uniswap publishes for RH. Which brand operates each is
    # still open, so the labels describe the mechanism rather than claim a pad.
    "0x23f8209572b4a1c2ad88a42749e830791fb027f1": "instant-launch-1",
    "0xad44d55e7f8337c3ce113fbb591486e85be104b2": "instant-launch-2",
    "0x05d552391067389ee44fec3924157ed33f976000": "lbp",
    "0x1242c9439d589cae85e121b1f79f2af51e91dcee": "universal-router",
    # Seen live on v3.0.0, not yet in any published deployment table.
    "0x9f67b864b565966dfcc2e0c6ba2483b2d5ff4b00": "strat-9f67b8",
    "0x544ef36801e90ee56bcd699ed51a63cfceac8ec9": "strat-544ef3",
}

# Live census, 40k blocks ending at block 31,247,303:
#   v3.2.0  212 instant-launch-1 | 100 universal-router | 10 instant-launch-2 | 8 lbp
#   v3.0.0    4 strat-9f67b8 | 1 strat-544ef3 | 1 lbp | 1 pools-trade
# Pons and flap tokens appear in NEITHER launcher — they run their own factories.
# Both launch events are now RECOVERED and wired into LAUNCHPADS above, so
# discover_fresh covers them: pons ~105 launches / 40k blocks, flap ~403.

# Shared infrastructure that shows up as a launch-tx `to` but is NOT a launchpad —
# never fingerprint these as a pad (they'd cluster unrelated launches together).
INFRA_ADDRS: set[str] = {
    "0x0000000071727de22e5e9d8baf0edac6f37da032",  # ERC-4337 EntryPoint v0.6
    "0xca11bde05977b3631167028862be2a173976ca11",  # Multicall3
    "0x8366a39cc670b4001a1121b8f6a443a643e40951",  # v4 PoolManager (self)
}


def _load_pad_names() -> None:
    """Merge user-supplied pad names from RH_PADS_FILE (JSON) so you can name every
    pad the census finds without editing code:
        {"hooks": {"0x..": "pons"}, "deployers": {"0x..": "noxa"},
         "suffixes": {"xyz": "flap"}}
    """
    path = os.getenv("RH_PADS_FILE", "")
    if not path:
        return
    try:
        import json
        with open(path) as fh:
            data = json.load(fh)
        PAD_BY_HOOK.update({k.lower(): v for k, v in data.get("hooks", {}).items()})
        PAD_BY_DEPLOYER.update({k.lower(): v for k, v in data.get("deployers", {}).items()})
        PAD_BY_SUFFIX.update({k.lower(): v for k, v in data.get("suffixes", {}).items()})
    except Exception:
        pass


_load_pad_names()


def pad_of(token: str | None, hook: str | None,
           deployer: str | None = None, strategy: str | None = None) -> str | None:
    """Which pad launched this token? Ordered by how forgeable each signal is:
    strategy (unspoofable — the launcher emitted it) > hook > deployer > address
    suffix (spoofable by a vanity grind, kept only as a last-resort hint).
    Otherwise a STABLE short fingerprint, so EVERY launch carries a label you can
    group by and name later. None only when there is nothing to fingerprint."""
    s = (strategy or "").lower()
    if s in PAD_BY_STRATEGY:
        return PAD_BY_STRATEGY[s]
    if s and s != ZERO_ADDR:
        return "strat:" + s[2:8]         # verified launch path, pad not yet named
    h = (hook or "").lower()
    if h in PAD_BY_HOOK:
        return PAD_BY_HOOK[h]
    d = (deployer or "").lower()
    if d in PAD_BY_DEPLOYER:
        return PAD_BY_DEPLOYER[d]
    if token:
        for suf, name in PAD_BY_SUFFIX.items():
            if token.lower().endswith(suf):
                return name
    if h and h != ZERO_ADDR:
        return "hook:" + h[2:8]          # unknown pad, but distinguishable by hook
    if d and d != ZERO_ADDR and d not in INFRA_ADDRS:
        return "dep:" + d[2:8]           # unknown pad, distinguishable by deployer
    return None                          # zero/infra deployer -> not a pad

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
