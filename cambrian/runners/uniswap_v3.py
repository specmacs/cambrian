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

WETH_DECIMALS = 18


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
