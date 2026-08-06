"""Uniswap v4 event decoding — catch fresh v4 pools (Pons v2 launches into v4).

v4 is a singleton PoolManager: pools are identified by a bytes32 PoolId, not an
address, and each carries a hooks contract. A pool is born on `Initialize`; swaps
are emitted by the PoolManager filtered by PoolId. Topic0s below were sourced
from the v4 ABI — verify against a real log if unsure.

    Initialize(PoolId indexed id, Currency indexed currency0,
               Currency indexed currency1, uint24 fee, int24 tickSpacing,
               IHooks hooks, uint160 sqrtPriceX96, int24 tick)
    Swap(PoolId indexed id, address indexed sender, int128 amount0,
         int128 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick,
         uint24 fee)

`amount0/1` are pool-balance deltas (same perspective as v3 — positive = token in
= a buy of the other side), so `aggregate_swaps` from the v3 module works here.
"""

from __future__ import annotations

from ..evm import mapping_slot, selector
from .uniswap_v3 import _addr, _signed, aggregate_swaps  # noqa: F401 (re-export)

# v4 stores each pool's state in `mapping(PoolId => Pool.State) _pools` at storage
# slot 6 (v4-core StateLibrary POOLS_SLOT). Within Pool.State: slot0 at offset +0
# (packed: sqrtPriceX96 | tick | ...), liquidity at +3. Read via extsload.
POOLS_SLOT = 6
EXTSLOAD = selector("extsload(bytes32)")   # 0x1e2eaeaf, keccak-derived

INITIALIZE_TOPIC0 = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
SWAP_TOPIC0 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
# v4 native ETH is Currency(address(0)) — pools pair against ETH or WETH.
NATIVE_ETH = "0x0000000000000000000000000000000000000000"


def _words(data: str, n: int) -> list[str]:
    data = data[2:] if data.startswith("0x") else data
    return [data[i * 64:(i + 1) * 64] for i in range(n)]


def decode_initialize(log: dict) -> dict:
    topics = log["topics"]
    w = _words(log["data"], 5)
    return {
        "pool_id": topics[1],                    # bytes32 PoolId
        "currency0": _addr(topics[2]),
        "currency1": _addr(topics[3]),
        "fee": int(w[0], 16),
        "tick_spacing": _signed(w[1]),
        "hooks": _addr(w[2]),                    # the hook contract (may be zero)
        "sqrt_price_x96": int(w[3], 16),
        "tick": _signed(w[4]),
    }


def decode_v4_swap(log: dict) -> dict:
    w = _words(log["data"], 6)
    return {
        "amount0": _signed(w[0]),
        "amount1": _signed(w[1]),
        "sqrtPriceX96": int(w[2], 16),
        "liquidity": int(w[3], 16),
        "tick": _signed(w[4]),
        "fee": int(w[5], 16),
    }


def _state_base_slot(pool_id_hex: str) -> int:
    pid = bytes.fromhex(pool_id_hex[2:] if pool_id_hex.startswith("0x") else pool_id_hex)
    return mapping_slot(pid.rjust(32, b"\x00"), POOLS_SLOT)


def read_pool_liquidity_sqrt(client, pool_manager: str, pool_id_hex: str):
    """(liquidity, sqrtPriceX96) for a v4 pool via extsload on the singleton.

    slot0 (packed) holds sqrtPriceX96 in its low 160 bits; liquidity is a uint128
    three slots later.
    """
    base = _state_base_slot(pool_id_hex)
    slot0 = int(client.eth_call(pool_manager, EXTSLOAD + base.to_bytes(32, "big").hex()), 16)
    liq_word = int(client.eth_call(pool_manager, EXTSLOAD + (base + 3).to_bytes(32, "big").hex()), 16)
    sqrt_price = slot0 & ((1 << 160) - 1)
    liquidity = liq_word & ((1 << 128) - 1)
    return liquidity, sqrt_price


def liquidity_usd_from(liquidity: int, sqrt_price_x96: int, quote_is_token0: bool,
                       quote_price_usd: float) -> float | None:
    """Estimate a v4 pool's USD depth from active liquidity L and price.

    Uses the constant-product virtual reserve on the quote side
    (token1: L*sqrt/2^96 ; token0: L*2^96/sqrt), x2 for both sides. It's an
    upper-bound proxy — concentrated liquidity has less real depth — but good
    enough for the rug/thin-pool filter.
    """
    if not liquidity or not sqrt_price_x96:
        return None
    q = (1 << 96)
    reserve = (liquidity * q / sqrt_price_x96) if quote_is_token0 \
        else (liquidity * sqrt_price_x96 / q)
    return reserve / 1e18 * quote_price_usd * 2
