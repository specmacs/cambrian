"""Uniswap V2 event decoding + constant-product math — where flap tokens trade.

This module exists because of a miss worth remembering: the desk scanned only v3
and v4, so it concluded flap launches "created no pool and were not buyable". They
are buyable — on **Uniswap V2**, which nothing here had looked at. Verified live:
186 of 186 flap `TokenCreated` events opened a V2 pair in the SAME transaction,
each seeded with ~1.9190 WETH, and 7 of 8 sampled pairs traded within minutes.

V2 differs from v3/v4 in three ways that matter here:

1. A pair is its own contract (not a singleton + PoolId, not an NFT position), so
   reserves are read with one `getReserves()` call — cheap and exact.
2. Liquidity is full-range constant product, so `x*y=k` gives an EXACT quote. This
   is strictly better than the v3/v4 depth proxies, which are upper bounds.
3. The `Swap` event carries four UNSIGNED amounts (in/out per side) rather than
   signed deltas. We fold them to the v3 pool-perspective convention
   (`amount = in - out`) so `aggregate_swaps` works unchanged, with invert=False.

    Swap(address indexed sender, uint amount0In, uint amount1In,
         uint amount0Out, uint amount1Out, address indexed to)
    Sync(uint112 reserve0, uint112 reserve1)
    PairCreated(address indexed token0, address indexed token1,
                address pair, uint256)

Every topic0 below is keccak256 of the signature above it, pinned by tests.
"""

from __future__ import annotations

from typing import Any

from .uniswap_v3 import WETH_DECIMALS, _addr, aggregate_swaps  # noqa: F401 (re-export)

PAIRCREATED_TOPIC0 = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
SWAP_TOPIC0 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
SYNC_TOPIC0 = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"

# getReserves() -> (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)
GET_RESERVES = "0x0902f1ac"

# Uniswap V2's canonical swap fee is 30 bps (the 997/1000 in the invariant). It is
# a constructor-level constant of the fork, not a per-pair value, so it cannot be
# read back from the pair — if RH's fork ever differs, override it at the call
# site rather than assuming this default is chain-verified.
DEFAULT_FEE_BPS = 30


def decode_pair_created(log: dict) -> dict:
    """Decode a V2 factory PairCreated log.

    token0/token1 are indexed; the data is (pair, allPairsLength).
    """
    topics = log["topics"]
    data = log["data"][2:] if log["data"].startswith("0x") else log["data"]
    return {
        "token0": _addr(topics[1]),
        "token1": _addr(topics[2]),
        "pair": _addr(data[0:64]),
        "index": int(data[64:128], 16) if len(data) >= 128 else None,
    }


def decode_v2_swap(log: dict[str, Any]) -> dict[str, int]:
    """Decode one V2 Swap into the SIGNED pool-perspective form v3 uses.

    V2 reports four unsigned legs; folding them to `in - out` per side yields the
    exact v3 convention (positive = token came INTO the pool), which is what
    `aggregate_swaps` expects. So V2 callers pass invert=False, like v3 — only v4
    inverts.
    """
    data = log["data"]
    data = data[2:] if data.startswith("0x") else data
    w = [int(data[i * 64:(i + 1) * 64], 16) for i in range(4)]
    a0_in, a1_in, a0_out, a1_out = w
    return {
        "amount0": a0_in - a0_out,
        "amount1": a1_in - a1_out,
        "amount0In": a0_in, "amount1In": a1_in,
        "amount0Out": a0_out, "amount1Out": a1_out,
    }


def read_reserves(client, pair: str) -> tuple[int, int] | None:
    """(reserve0, reserve1) for a V2 pair, or None if unreadable.

    Returns None rather than raising or zero-filling: a pair whose reserves cannot
    be read is an UNKNOWN, and the scorer fails closed on unknowns. Silently
    reporting 0 would read as "no liquidity" and could equally read as "safe".
    """
    raw = client.eth_call(pair, GET_RESERVES)
    if not raw:
        return None
    body = raw[2:] if raw.startswith("0x") else raw
    if len(body) < 128:
        return None
    return int(body[0:64], 16), int(body[64:128], 16)


def read_real_backing(client, pair: str, quote_token: str) -> int | None:
    """Quote asset the pair ACTUALLY holds, via `balanceOf(pair)`.

    Needed because on this chain `getReserves()` is not proof of assets. flap's
    pairs are beacon proxies that report VIRTUAL reserves for V2-compatible
    tooling while the real assets sit in the Portal: a live pair reported 1.9190
    WETH of reserve against `balanceOf(pair) == 0` for both legs, with the Portal
    holding 45 native ETH and ~100% of token supply. Pre-graduation those pairs
    are accounting shells over a bonding curve.

    A canonical Uniswap V2 pair returns its true balance here and agrees with
    `getReserves()`; a shell returns 0. That difference is the test.
    """
    try:
        raw = client.eth_call(quote_token,
                              "0x70a08231" + pair[2:].rjust(64, "0"))
    except Exception:
        return None
    if not raw or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return None


def liquidity_usd_from_reserves(reserves: tuple[int, int] | None, *,
                                quote_is_token0: bool,
                                quote_price_usd: float,
                                real_backing: int | None = None) -> float | None:
    """Pool depth in USD from the quote side, doubled.

    Pass `real_backing` (from `read_real_backing`) whenever you can. Without it
    this trusts `getReserves()`, which on a flap pair is VIRTUAL — reporting
    ~$11.5k of depth that the pair does not hold. With it, the reported reserve is
    capped at what is really there, so a shell reads as 0 depth rather than as a
    healthy pool. That distinction decides whether a position can actually be
    exited, so prefer passing it even at the cost of one extra call.
    """
    if not reserves and real_backing is None:
        return None
    quote = None
    if reserves:
        quote = reserves[0] if quote_is_token0 else reserves[1]
    if real_backing is not None:
        quote = real_backing if quote is None else min(quote, real_backing)
    if quote is None:
        return None
    return quote / (10 ** WETH_DECIMALS) * quote_price_usd * 2


def amount_out(amount_in: int, reserve_in: int, reserve_out: int, *,
               fee_bps: int = DEFAULT_FEE_BPS) -> int:
    """Constant-product output for an exact input — the V2 invariant.

        out = (in * (1-fee) * reserveOut) / (reserveIn + in * (1-fee))

    Exact, not an estimate, so it doubles as the sell-quote used to detect a
    honeypot: if this says a sell should return value and the real call reverts,
    the token is not sellable.
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    net = amount_in * (10_000 - fee_bps)
    return (net * reserve_out) // (reserve_in * 10_000 + net)


def price_in_quote(reserves: tuple[int, int] | None, *, quote_is_token0: bool,
                   token_decimals: int = 18) -> float | None:
    """Spot price of the non-quote token, denominated in the quote token."""
    if not reserves:
        return None
    quote, tok = (reserves[0], reserves[1]) if quote_is_token0 else (reserves[1], reserves[0])
    if tok == 0:
        return None
    return (quote / 10 ** WETH_DECIMALS) / (tok / 10 ** token_decimals)


def sell_value_quote(token_amount: int, reserves: tuple[int, int] | None, *,
                     quote_is_token0: bool, fee_bps: int = DEFAULT_FEE_BPS,
                     tax_bps: int = 0) -> float | None:
    """What selling `token_amount` would actually return, in quote units.

    Prices the trade against real depth including slippage, and applies the
    token's sell tax — a 10% tax presents as slippage and would otherwise be
    silently absorbed into a "bad fill" rather than flagged as a bad token.
    """
    if not reserves:
        return None
    reserve_quote, reserve_tok = (reserves[0], reserves[1]) if quote_is_token0 \
        else (reserves[1], reserves[0])
    taxed_in = token_amount * (10_000 - max(tax_bps, 0)) // 10_000
    out = amount_out(taxed_in, reserve_tok, reserve_quote, fee_bps=fee_bps)
    return out / (10 ** WETH_DECIMALS)
