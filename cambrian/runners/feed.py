"""Live data feed for the runner tracker (RPC + Blockscout). Best-effort seam.

`discover_fresh` watches the configured launchpad factories for creation events
and returns the new (token, pool, launchpad) refs. Turning those into full
`RunnerCandidate`s — liquidity, holders, volume, smart-money buyers — is
`enrich`, which needs each pad's event ABI and the explorer's token/holder
endpoints; those are marked below and filled once you have the addresses.

Nothing here is on a trading path, and the scorer is fully testable without any
of it — this is the wiring, not the brain.
"""

from __future__ import annotations

from typing import Any

from ..chain import ChainClient
from . import config as rcfg
from .snapshots import RunnerCandidate
from .uniswap_v3 import (POOLCREATED_TOPIC0, SWAP_TOPIC0, aggregate_swaps,
                         decode_pool_created, decode_v3_swap)


def find_contracts(client: ChainClient, *, from_block: str,
                   to_block: str = "latest") -> dict[str, dict[str, int]]:
    """Self-bootstrap the DEX contract addresses from the chain itself.

    Every V3 `PoolCreated` log is emitted by the V3 factory, and every v4
    `Initialize` log by the PoolManager — so scanning those topic0s *unfiltered
    by address* and tallying emitters reveals both. The most frequent address per
    event is the real contract (confirms the V3 factory, discovers the v4
    PoolManager). No address needed up front.
    """
    from .uniswap_v3 import POOLCREATED_TOPIC0
    from .uniswap_v4 import INITIALIZE_TOPIC0
    out: dict[str, dict[str, int]] = {}
    for key, topic in (("v3_factory", POOLCREATED_TOPIC0),
                       ("pool_manager", INITIALIZE_TOPIC0)):
        counts: dict[str, int] = {}
        for log in client.get_logs(topics=[topic], from_block=from_block,
                                   to_block=to_block):
            a = (log.get("address") or "").lower()
            if a:
                counts[a] = counts.get(a, 0) + 1
        out[key] = counts
    return out


def discover_new_pools(client: ChainClient, *, v3_factory: str, weth: str,
                       from_block: str, to_block: str = "latest") -> list[dict]:
    """Every fresh WETH-paired V3 pool created in the window — pad-agnostic.

    Watches the V3 factory's PoolCreated event, so it catches launches from any
    launchpad (Pons, Noxa, Pools.trade, …) without needing each pad's custom
    event ABI. Returns {token, pool, fee, weth_is_token0} per new pool.
    """
    weth_l = weth.lower()
    out: list[dict] = []
    for log in client.get_logs(address=v3_factory, topics=[POOLCREATED_TOPIC0],
                               from_block=from_block, to_block=to_block):
        d = decode_pool_created(log)
        t0, t1 = d["token0"].lower(), d["token1"].lower()
        if weth_l not in (t0, t1):
            continue
        blk = log.get("blockNumber")
        out.append({
            "token": d["token1"] if t0 == weth_l else d["token0"],
            "pool": d["pool"],
            "fee": d["fee"],
            "weth_is_token0": t0 == weth_l,
            "block": int(blk, 16) if isinstance(blk, str) else blk,
        })
    return out


def discover_fresh(client: ChainClient, *, from_block: str, to_block: str = "latest",
                   launchpads: dict | None = None) -> list[dict[str, Any]]:
    """Return raw creation-event logs across the watched launchpads.

    Each pad needs its factory `address` and the `created_topic0` of its
    token/pool-created event (from the verified contract). Pads not yet filled
    in are skipped, so an empty LAUNCHPADS returns nothing (fail closed).
    """
    launchpads = rcfg.LAUNCHPADS if launchpads is None else launchpads
    hits: list[dict[str, Any]] = []
    for name, pad in launchpads.items():
        addr, topic0 = pad.get("address"), pad.get("created_topic0")
        if not addr or not topic0:
            continue
        for log in client.get_logs(address=addr, topics=[topic0],
                                    from_block=from_block, to_block=to_block):
            hits.append({"launchpad": name, "factory": addr, "log": log})
    return hits


def discover_new_pools_v4(client: ChainClient, *, pool_manager: str, weth: str,
                          from_block: str, to_block: str = "latest",
                          native_eth: str | None = None) -> list[dict]:
    """Fresh v4 pools paired against WETH or native ETH — with their hook.

    v4 pools are PoolIds inside the singleton PoolManager, born on `Initialize`.
    Returns {token, pool_id, hooks, fee, quote, quote_is_token0, block}. The
    hook is captured because a v4 pool IS its hook — the degen desk reviews it.
    """
    from .uniswap_v4 import INITIALIZE_TOPIC0, NATIVE_ETH, decode_initialize
    native = (native_eth or NATIVE_ETH).lower()
    quotes = {weth.lower(), native}
    out: list[dict] = []
    for log in client.get_logs(address=pool_manager, topics=[INITIALIZE_TOPIC0],
                               from_block=from_block, to_block=to_block):
        d = decode_initialize(log)
        c0, c1 = d["currency0"].lower(), d["currency1"].lower()
        if not (quotes & {c0, c1}):
            continue
        quote_is_c0 = c0 in quotes
        blk = log.get("blockNumber")
        out.append({
            "token": d["currency1"] if quote_is_c0 else d["currency0"],
            "pool_id": d["pool_id"],
            "hooks": d["hooks"],
            "fee": d["fee"],
            "quote": "ETH" if native in {c0, c1} else "WETH",
            "quote_is_token0": quote_is_c0,
            "block": int(blk, 16) if isinstance(blk, str) else blk,
        })
    return out


def v4_pool_swap_metrics(client: ChainClient, *, pool_manager: str, pool_id: str,
                         from_block: str, to_block: str, quote_is_token0: bool,
                         quote_price_usd: float) -> dict[str, float]:
    """Volume + buy/sell flow for a v4 pool, filtered by PoolId on the manager."""
    from .uniswap_v4 import SWAP_TOPIC0, decode_v4_swap
    logs = client.get_logs(address=pool_manager, topics=[SWAP_TOPIC0, pool_id],
                           from_block=from_block, to_block=to_block)
    swaps = [decode_v4_swap(lg) for lg in logs]
    return aggregate_swaps(swaps, weth_is_token0=quote_is_token0,
                           weth_price_usd=quote_price_usd)


def pool_swap_metrics(client: ChainClient, pool: str, *, from_block: str,
                      to_block: str, weth_is_token0: bool,
                      weth_price_usd: float) -> dict[str, float]:
    """Volume + buy/sell counts for a V3 pool over a block window.

    This half is fully wired — V3 `Swap` has a standard ABI and topic0, so it
    works against any RH V3 pool once you have the pool address and WETH price.
    """
    logs = client.get_logs(address=pool, topics=[SWAP_TOPIC0],
                           from_block=from_block, to_block=to_block)
    swaps = [decode_v3_swap(log) for log in logs]
    return aggregate_swaps(swaps, weth_is_token0=weth_is_token0,
                           weth_price_usd=weth_price_usd)


def enrich(client: ChainClient, hit: dict[str, Any], *, weth_usd: float,
           latest_block: int, now_ts: int | None, window_blocks: int,
           bs_client: Any = None) -> RunnerCandidate:
    """Assemble a full RunnerCandidate for a discovered pool.

    Defensive: every metric is best-effort, and anything that fails stays None —
    the scorer already fails closed on unknown rug facts, so a partial fetch
    can't accidentally flag a token as safe.

    Not populated live (degrade gracefully): holders_5m_ago (no cheap point-in-
    time holder count) and lp_locked (V3 liquidity is an NFT position; Pons
    auto-locks — verify per pad). hook is v4-only.
    """
    token, pool = hit["token"], hit["pool"]
    weth_is_token0 = hit["weth_is_token0"]
    recent_from = hex(max(latest_block - window_blocks, 0))
    prior_from = hex(max(latest_block - 2 * window_blocks, 0))
    prior_to = hex(max(latest_block - window_blocks - 1, 0))

    def _swaps(fb, tb):
        try:
            return pool_swap_metrics(client, pool, from_block=fb, to_block=tb,
                                     weth_is_token0=weth_is_token0,
                                     weth_price_usd=weth_usd)
        except Exception:
            return {"volume_usd": None, "buys": None, "sells": None}

    m5 = _swaps(recent_from, "latest")
    mp = _swaps(prior_from, prior_to)

    liquidity_usd = None
    try:
        weth = rcfg.CONTRACTS["weth"]
        bal = client.erc20_balance_of(weth, pool) / 1e18
        liquidity_usd = bal * weth_usd * 2  # WETH side x2 as a total-depth proxy
    except Exception:
        pass

    symbol = holders = top_holder = None
    if bs_client is not None:
        try:
            tj, hj = bs_client.token(token), bs_client.holders(token)
            from .blockscout import holder_count, top_holder_pct
            symbol = tj.get("symbol")
            holders = holder_count(tj)
            top_holder = top_holder_pct(tj, hj)
        except Exception:
            pass

    age_minutes = None
    if hit.get("block") and now_ts:
        try:
            ts = client.block_timestamp(hex(hit["block"]))
            if ts:
                age_minutes = (now_ts - ts) / 60.0
        except Exception:
            pass

    smart = 0
    if rcfg.WATCHED_WALLETS:
        try:
            logs = client.get_logs(address=pool, topics=[SWAP_TOPIC0],
                                   from_block=recent_from, to_block="latest")
            watched = {w.lower() for w in rcfg.WATCHED_WALLETS}
            recipients = {("0x" + lg["topics"][2][-40:]).lower()
                          for lg in logs if len(lg.get("topics", [])) > 2}
            smart = len(recipients & watched)
        except Exception:
            smart = 0

    return RunnerCandidate(
        token=token, pool=pool, launchpad=None, symbol=symbol,
        age_minutes=age_minutes, liquidity_usd=liquidity_usd,
        top_holder_pct=top_holder, holders=holders, holders_5m_ago=None,
        volume_5m_usd=m5["volume_usd"], volume_prior_5m_usd=mp["volume_usd"],
        buys_5m=m5["buys"], sells_5m=m5["sells"], smart_money_buyers=smart,
        lp_locked=None, hook=None,
    )


def _holders_age(client, token, hit, now_ts, bs_client):
    """Shared: (symbol, holders, top_holder_pct, age_minutes) — best-effort."""
    symbol = holders = top = None
    if bs_client is not None:
        try:
            tj, hj = bs_client.token(token), bs_client.holders(token)
            from .blockscout import holder_count, top_holder_pct
            symbol, holders, top = tj.get("symbol"), holder_count(tj), top_holder_pct(tj, hj)
        except Exception:
            pass
    age = None
    if hit.get("block") and now_ts:
        try:
            ts = client.block_timestamp(hex(hit["block"]))
            if ts:
                age = (now_ts - ts) / 60.0
        except Exception:
            pass
    return symbol, holders, top, age


def enrich_v4(client: ChainClient, hit: dict[str, Any], *, weth_usd: float,
              latest_block: int, now_ts: int | None, window_blocks: int,
              pool_manager: str, bs_client: Any = None) -> RunnerCandidate:
    """Full candidate for a discovered v4 pool. Liquidity via extsload on the
    singleton; volume via PoolId-filtered Swap logs. `pool` carries the PoolId."""
    from .uniswap_v4 import liquidity_usd_from, read_pool_liquidity_sqrt
    token, pid = hit["token"], hit["pool_id"]
    q_is0 = hit["quote_is_token0"]
    recent_from = hex(max(latest_block - window_blocks, 0))
    prior_from = hex(max(latest_block - 2 * window_blocks, 0))
    prior_to = hex(max(latest_block - window_blocks - 1, 0))

    def _sw(fb, tb):
        try:
            return v4_pool_swap_metrics(client, pool_manager=pool_manager, pool_id=pid,
                                        from_block=fb, to_block=tb, quote_is_token0=q_is0,
                                        quote_price_usd=weth_usd)
        except Exception:
            return {"volume_usd": None, "buys": None, "sells": None}

    m5, mp = _sw(recent_from, "latest"), _sw(prior_from, prior_to)
    liquidity = None
    try:
        liq, sqrtp = read_pool_liquidity_sqrt(client, pool_manager, pid)
        liquidity = liquidity_usd_from(liq, sqrtp, q_is0, weth_usd)
    except Exception:
        pass
    symbol, holders, top, age = _holders_age(client, token, hit, now_ts, bs_client)
    return RunnerCandidate(
        token=token, pool=pid, launchpad=None, symbol=symbol, age_minutes=age,
        liquidity_usd=liquidity, top_holder_pct=top, holders=holders,
        holders_5m_ago=None, volume_5m_usd=m5["volume_usd"],
        volume_prior_5m_usd=mp["volume_usd"], buys_5m=m5["buys"], sells_5m=m5["sells"],
        smart_money_buyers=0, lp_locked=None, hook=hit["hooks"],
    )


def scan_live(client: ChainClient, *, blocks: int, weth_usd: float,
              window_blocks: int, bs_client: Any = None,
              v3_factory: str | None = None, weth: str | None = None,
              pool_manager: str | None = None, native_eth: str | None = None
              ) -> list[RunnerCandidate]:
    """Discover fresh WETH/ETH pools (v3 + v4) over the last `blocks`, enrich each
    to a scored-ready candidate. The full live pipeline."""
    v3_factory = v3_factory or rcfg.CONTRACTS["v3_factory"]
    weth = weth or rcfg.CONTRACTS["weth"]
    latest = client.block_number()
    now_ts = client.block_timestamp("latest")
    from_block = hex(max(latest - blocks, 0))

    hits = discover_new_pools(client, v3_factory=v3_factory, weth=weth,
                              from_block=from_block)
    out = [enrich(client, h, weth_usd=weth_usd, latest_block=latest, now_ts=now_ts,
                  window_blocks=window_blocks, bs_client=bs_client) for h in hits]

    if pool_manager:
        v4 = discover_new_pools_v4(client, pool_manager=pool_manager, weth=weth,
                                   native_eth=native_eth, from_block=from_block)
        out += [enrich_v4(client, h, weth_usd=weth_usd, latest_block=latest,
                          now_ts=now_ts, window_blocks=window_blocks,
                          pool_manager=pool_manager, bs_client=bs_client) for h in v4]
    return out
