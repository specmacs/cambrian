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
from .uniswap_v3 import SWAP_TOPIC0, aggregate_swaps, decode_v3_swap


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


def enrich(client: ChainClient, hit: dict[str, Any]) -> RunnerCandidate:  # pragma: no cover
    """Turn a raw creation log into a full candidate.

    Wired already: volume + buys/sells via `pool_swap_metrics` (standard V3
    Swap ABI). Remaining seams, per launchpad:
      * decode the created event -> token + V3 pool address (needs the pad's
        created-event layout; that's why `created_topic0` must be filled)
      * liquidity_usd (pool WETH balance x price), weth_price_usd
      * holders, holders_5m_ago, top_holder_pct (Blockscout token-holders API)
      * smart_money_buyers (WATCHED_WALLETS among recent buy `recipient`s)
      * lp_locked (LP-token holder is a known locker / burn address)
    Until those are wired, this raises so nothing silently scores on empty data.
    """
    raise NotImplementedError(
        "enrich() still needs the pad's created-event layout + the explorer's "
        "token/holder endpoints + WETH price. Swap volume/flow is wired "
        "(pool_swap_metrics). Fill created_topic0 and wire those, or feed the "
        "scorer from a fixture (see `runners --fixture`)."
    )
