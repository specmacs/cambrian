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
# 40k-block window — one each per token). The TokenLaunched field names below are
# from Pons's official docs (docs.ponsfamily.com), which publish this exact
# topic0 — an independent confirmation of the keccak we derived:
#   TokenLaunched(address indexed token, address indexed deployer,
#                 address indexed dexFactory, address pairToken, address pool,
#                 uint256 dexId, uint256 launchConfigId, uint256 positionId,
#                 uint256 restrictionsEndBlock, uint256 initialBuyAmount)
#   TokenDeployed(address,address,address,address,uint256,uint256)  (same layout head)
# NOTE topic2 is the DEPLOYER, not the pool — the pool is the 2nd data word. An
# earlier reading here had those two swapped. `initialBuyAmount` (last data word)
# is the creator's own opening buy in wei, observed 0, 0.2e18 and 3.5e18, and
# `restrictionsEndBlock` is when launch restrictions lift — both are entry signal
# available at t=0. Pons's docs recommend exactly this: index TokenLaunched off
# the factory, then index each emitted pool's Swap events.
PONS_FACTORY = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"          # start 8991118
PONS_FACTORY_LEGACY = "0x0c37a24F5D23A486FA692d1500881d698B1F77a4"   # start 8600612
PONS_LOCKER = "0x736D76699C26D0d966744cAe304C000d471f7F35"
EVT_PONS_TOKEN_DEPLOYED = "0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a"
EVT_PONS_TOKEN_LAUNCHED = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"

# flap's Portal emits these. Both signatures come from flap's official docs
# (docs.flap.sh) and are keccak-PROVEN against the live topic0s. All params are
# unindexed, so they match on emitter+topic0 — still unforgeable, since only the
# Portal can emit at the Portal's address. ~403 launches / 40k blocks.
#   TokenCreated(uint256 ts, address creator, uint256 nonce, address token,
#                string name, string symbol, string meta)
# `meta` is an IPFS CID, so name/symbol/metadata all arrive with the launch and
# need no extra eth_call.
EVT_FLAP_LAUNCH = "0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603"
#   TokenCurveSetV2(address token, uint256 seedWeth, uint256, uint256)
# Fires in the same tx. `seedWeth` is the WETH seeded into the pair — observed at
# a constant 1.9190 WETH across every sampled launch.
EVT_FLAP_CURVE = "0x71a10912a55f73d3cced0d1515c2b33c396c80342522bad0e295ccbede556f37"
EVT_FLAP_LIQUIDITY = EVT_FLAP_CURVE       # back-compat alias, pre-docs name
# Documented graduation event, LaunchedToDEX(address,address,uint256,uint256).
# ZERO occurrences anywhere on this chain across 200k blocks — on Robinhood Chain
# flap does not run the bonding-curve-then-graduate path; it opens the V2 pair at
# launch (see below). Kept so a future session does not re-derive it.
EVT_FLAP_LAUNCHED_TO_DEX = "0x6e4f47630b8745b8cacbd44f42a8a33e7eea7cc08ef22fc7630f4f385784ff7d"

FLAP_PORTAL = "0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09"          # docs-confirmed
FLAP_VAULT_PORTAL = "0xe9F7AB7DE8FB8756acbB6a1cd13316a43308197B"
FLAP_TOKEN_IMPL = "0x88882688a067FE97E11C2185b996286e53132222"       # TOKEN_V2_PERMIT
FLAP_TAX_TOKEN_V3_IMPL = "0x7777C8743C88B3aff3cf262135beF2c8b2e83333"  # TOKEN_TAXED_V3

# --- Uniswap V2 on RH: where flap tokens actually trade ----------------------
# CHAIN-VERIFIED and it overturns an earlier conclusion in this repo. We reported
# that flap launches "create no pool and are not buyable at launch" — that was an
# artefact of only ever scanning v3 and v4. flap launches into Uniswap **V2**:
# in a 10k-block window, 186 of 186 flap TokenCreated events created a V2 pair IN
# THE SAME TX. Every sampled pair was seeded with 1.9190 WETH, and 7 of 8 sampled
# pairs had real swaps within minutes. flap is the highest-volume launch source on
# the chain, so a desk that ignores V2 ignores most of the market.
V2_FACTORY = "0x0d1ebb179cdbca88d74c923c4255cb2b17474afd"   # 640 pairs / 40k blocks
# Two other V2-style factories also emit, far smaller; unidentified for now.
V2_FACTORIES_OTHER = ("0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",   # 15 / 40k
                      "0xfc2e4da3edb2e18100473339c763705d263d20a9")   # 6 / 40k
EVT_V2_PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
EVT_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
EVT_V2_SYNC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"

# flap TAX TOKENS. The public docs describe a 1/3/5/10% menu; the chain disagrees.
# Reading buyTaxRate()/sellTaxRate() across 120 consecutive live launches:
#   10.0% x74 | 7.3% x11 | 6.3% x6 | 4.3% x5 | 1.3% x5 | 9.3%/8.3%/5.3%/3.3% x4
#    3.0% x1  | 2.3% x1  | 1.0% x1
# So rates are arbitrary, not tiered, and 62% of flap launches carry the maximum
# 10%. Gate on the number, never on a known-tier set. Owner's hard rule is 3% —
# see runners/flap_tax.py, which enforces it. Post-gate, only ~7% of flap flow is
# tradable, so flap's headline launch rate is NOT its tradable rate.
FLAP_TAX_HELPER = "0xb10bD2672aE63735d677164A54B573a016f0203C"
FLAP_MAX_TAX_BPS = 300

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
    # deployed FRONG's pools, then concluded it was "a token in the launch". Both
    # readings were wrong: Uniswap's published v4 deployment table for chain 4663
    # lists 0x58daec3116aae6d93017baaea7749052e8a04fa7 as the **PositionManager**.
    # It is core v4 infrastructure, which is exactly why it shows up in launch txs.
    # Right to remove, wrong reason — see POOLS_TRADE["position_manager"].
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
    "v3.2.0": "0x0000fffFbe8efe702c8703ae3477ff5de3d319c0",   # 306 dists / 40k blocks
    "v3.0.0": "0x00004c4ccc709ef590f7c81102c0689f0263d4e9",   # 8 dists / 40k blocks
}
UNI_LAUNCHER = UNI_LAUNCHERS["v3.2.0"]                        # back-compat alias

# --- This launcher stack IS pools.trade -------------------------------------
# pools.trade is Uniswap Labs' own launchpad (live 2026-08-05) and it runs on
# Uniswap's own contracts, every one of them source-verified on Blockscout under
# these names. So "came through the launcher" is a far stronger identification
# than we credited it with.
#
# IMPORTANT correction to correction #2. We had removed the launcher from
# PAD_BY_DEPLOYER on the reasoning that it is "shared infrastructure that EVERY
# pad routes through". Removing it was right, but that premise is false: sampling
# recent launches, 0 of 24 Pons and flap launches touched the LiquidityLauncher
# at all — those pads run their own factories end to end. Keep the launcher out
# of the pad maps because it is a launcher, not because everyone uses it.
#
# pools.trade offers exactly two formats, and both are pinned on-chain:
#   Instant Launch  -> InstantLaunchStrategy, token tradable immediately
#   Crowd Launch    -> LBPStrategy, 4h of bids via the auction factory, then a pool
# The Crowd mapping is PROVEN, not inferred: in 12 of 12 sampled AuctionCreated
# transactions the token was distributed through LBPStrategy, and the auction
# factory is the sole emitter of AuctionCreated on this chain.
#
# Launcher launches settle into HOOKLESS v4 pools (hooks == address(0)), which is
# how they are told apart from hooked pads sharing the same PoolManager.
POOLS_TRADE = {
    "launcher_v3_2_0": "0x0000fffFbe8efe702c8703ae3477ff5de3d319c0",  # LiquidityLauncher
    "launcher_v3_0_0": "0x00004c4ccc709ef590f7c81102c0689f0263d4e9",  # LiquidityLauncher
    "cca_factory": "0x000000001f26a0044baa66024e7b6599c61963f8",  # ContinuousClearingAuctionFactory
    "instant_strategy_1": "0x23f8209572b4a1c2ad88a42749e830791fb027f1",  # InstantLaunchStrategy
    "instant_strategy_2": "0xad44d55e7f8337c3ce113fbb591486e85be104b2",  # InstantLaunchStrategy
    "lbp_strategy": "0x05d552391067389ee44fec3924157ed33f976000",        # LBPStrategy
    "universal_router_strategy": "0x1242c9439d589cae85e121b1f79f2af51e91dcee",  # UniversalRouterStrategy
    "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",   # v4, hookless pools
    "position_manager": "0x58daec3116aae6d93017baaea7749052e8a04fa7",
}

EVT_TOKEN_CREATED = "0x2e2b3f61b70d2d131b2a807371103cc98d51adcaa5e9a8f9c32658ad8426e74e"
EVT_TOKEN_DISTRIBUTED = "0x67226bacccef969dab310a9e55dc1cf821363658e433fd330344f5cc00c79ac8"
EVT_AUCTION_CREATED = "0x7ede475fad18ccf0039f2b956c4d43a8b4ed0853de4daaa8ae25299f331ae3b9"

# TokenDistributed(token, strategy, amount): the strategy is the pad's real
# identity — each pad runs its own strategy/fee-splitter pair. Names are the
# contract roles from the repo; which pad operates which is still being mapped
# by examples/rh_launcher.py, so these stay descriptive rather than branded.
# Every name below is now the contract's OWN source-verified name on Blockscout,
# not our guess — the descriptive labels we chose turned out to match Uniswap's
# actual contract names exactly. They still describe a launch MECHANISM rather
# than a brand, which stays the right way to read them: all of these strategies
# belong to the pools.trade stack (see POOLS_TRADE above), so the useful question
# is which format a token launched under, not which pad "owns" the strategy.
PAD_BY_STRATEGY: dict[str, str] = {
    # CONFIRMED: FRONG (0x6245e6..0c47), the pools.trade flagship, was distributed
    # through this strategy on the v3.0.0 launcher. Chain-verified, not inferred.
    # This one is NOT source-verified on Blockscout — the only unverified strategy
    # we have seen, so treat the label as resting on the FRONG trace alone.
    "0x60d73b21cdf2ea846ab3d58699bbbb8f29d72491": "pools-trade",
    # `InstantLaunchStrategy` (both) — pools.trade "Instant Launch": tradable at once.
    "0x23f8209572b4a1c2ad88a42749e830791fb027f1": "instant-launch-1",   # 184/40k
    "0xad44d55e7f8337c3ce113fbb591486e85be104b2": "instant-launch-2",   # 11/40k
    # `LBPStrategy` — pools.trade "Crowd Launch". PROVEN: 12/12 sampled
    # AuctionCreated txs distributed through this strategy.
    "0x05d552391067389ee44fec3924157ed33f976000": "lbp",                # 12/40k
    # `UniversalRouterStrategy`. NB this is the strategy contract, NOT Uniswap's
    # Universal Router itself (that is 0x88767899..0904) — do not conflate them.
    "0x1242c9439d589cae85e121b1f79f2af51e91dcee": "universal-router",   # 96/40k
    # Seen live on v3.0.0, not yet in any published deployment table.
    "0x9f67b864b565966dfcc2e0c6ba2483b2d5ff4b00": "strat-9f67b8",
    "0x544ef36801e90ee56bcd699ed51a63cfceac8ec9": "strat-544ef3",
    "0xce57498d3474dcc244dfb6710ffbe6d4441cd2b2": "strat-ce5749",       # 3/40k, unverified
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
