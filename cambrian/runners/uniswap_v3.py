"""Uniswap V3 event decoding — the metric engine for pad runners.

Pons and Noxa launch tokens into Uniswap V3 quoted against WETH, so a pool's
`Swap` logs are where volume and buy/sell pressure live. This decodes those logs
and aggregates them into the numbers the runner scorer wants. Pure — the log
fetching (client.get_logs) is the caller's; the math here is tested offline.

A V3 Swap event:
    Swap(address indexed sender, address indexed recipient,
         int256 amount0, int256 amount1, uint160 sqrtPriceX96,
         uint128 liquidity, int24 tick)
sender/recipient are indexed (topics); the other five are the 160-byte data.
`amountN` is signed from the POOL's perspective: positive = that token came INTO
the pool, negative = it went OUT. So WETH-in (positive WETH amount) == a buy of
the other token.
"""

from __future__ import annotations

from typing import Any

# keccak256("Swap(address,address,int256,int256,uint160,uint128,int24)") — the
# canonical Uniswap V3 Swap event topic0. Verify against a real log if unsure.
SWAP_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# keccak256("PoolCreated(address,address,uint24,int24,address)") — V3 factory's
# new-pool event. Watching this catches EVERY fresh V3 pool regardless of which
# launchpad minted the token. Verify against a real log if unsure.
POOLCREATED_TOPIC0 = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

WETH_DECIMALS = 18


def _addr(topic_or_word: str) -> str:
    """Last 20 bytes of a 32-byte word -> checksummed-length lowercase address."""
    h = topic_or_word[2:] if topic_or_word.startswith("0x") else topic_or_word
    return "0x" + h[-40:]


def decode_pool_created(log: dict) -> dict:
    """Decode a V3 factory PoolCreated log.

    token0, token1, fee are indexed (topics 1-3); tickSpacing + pool are the data.
    """
    topics = log["topics"]
    data = log["data"][2:] if log["data"].startswith("0x") else log["data"]
    return {
        "token0": _addr(topics[1]),
        "token1": _addr(topics[2]),
        "fee": int(topics[3], 16),
        "tickSpacing": _signed(data[0:64]),
        "pool": _addr(data[64:128]),
    }


def price0_in_1(sqrt_price_x96: int, decimals0: int, decimals1: int) -> float:
    """Human-unit price of token0 denominated in token1, from sqrtPriceX96.

        price = (sqrtPriceX96 / 2**96)**2 * 10**(decimals0 - decimals1)
    """
    ratio = (sqrt_price_x96 / (1 << 96)) ** 2
    return ratio * (10 ** (decimals0 - decimals1))


def _signed(word_hex: str) -> int:
    v = int(word_hex, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def decode_v3_swap(log: dict[str, Any]) -> dict[str, int]:
    """Decode one V3 Swap log's data into its five fields."""
    data = log["data"]
    data = data[2:] if data.startswith("0x") else data
    words = [data[i * 64:(i + 1) * 64] for i in range(5)]
    return {
        "amount0": _signed(words[0]),
        "amount1": _signed(words[1]),
        "sqrtPriceX96": int(words[2], 16),
        "liquidity": int(words[3], 16),
        "tick": _signed(words[4]),   # int24, sign-extended to 32 bytes
    }


def aggregate_swaps(swaps: list[dict[str, int]], *, weth_is_token0: bool,
                    weth_price_usd: float) -> dict[str, float]:
    """Roll decoded swaps up into volume (USD) and buy/sell counts.

    Volume is measured on the WETH leg (the priced side): |WETH moved| × price.
    """
    volume_usd = 0.0
    buys = sells = 0
    for s in swaps:
        weth_amt = s["amount0"] if weth_is_token0 else s["amount1"]
        volume_usd += abs(weth_amt) / (10 ** WETH_DECIMALS) * weth_price_usd
        if weth_amt > 0:      # WETH into the pool -> someone bought the token
            buys += 1
        elif weth_amt < 0:
            sells += 1
    return {"volume_usd": volume_usd, "buys": buys, "sells": sells}
