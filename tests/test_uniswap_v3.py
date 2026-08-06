from cambrian.runners.uniswap_v3 import aggregate_swaps, decode_v3_swap


def _w(v: int) -> str:
    """Encode a signed int as a 32-byte two's-complement hex word."""
    return format(v & ((1 << 256) - 1), "064x")


def _swap_log(amount0, amount1, tick=0):
    data = "0x" + _w(amount0) + _w(amount1) + _w(0) + _w(0) + _w(tick)
    return {"data": data}


def test_decode_handles_signed_amounts_and_tick():
    log = _swap_log(-1000, 2 * 10**18, tick=-42)
    d = decode_v3_swap(log)
    assert d["amount0"] == -1000
    assert d["amount1"] == 2 * 10**18
    assert d["tick"] == -42


def test_aggregate_counts_buys_and_sells_and_volume():
    # WETH is token1. amount1 > 0 = WETH in = buy; < 0 = sell.
    swaps = [
        {"amount0": -100, "amount1": 1 * 10**18},   # buy, 1 WETH
        {"amount0": -50, "amount1": 3 * 10**18},     # buy, 3 WETH
        {"amount0": 200, "amount1": -2 * 10**18},    # sell, 2 WETH
    ]
    out = aggregate_swaps(swaps, weth_is_token0=False, weth_price_usd=3000.0)
    assert out["buys"] == 2 and out["sells"] == 1
    # volume on the WETH leg: (1 + 3 + 2) WETH * $3000 = $18,000
    assert out["volume_usd"] == 6 * 3000.0


def test_aggregate_respects_weth_is_token0():
    swaps = [{"amount0": 5 * 10**18, "amount1": -100}]  # WETH(token0) in = buy
    out = aggregate_swaps(swaps, weth_is_token0=True, weth_price_usd=2000.0)
    assert out["buys"] == 1 and out["sells"] == 0
    assert out["volume_usd"] == 5 * 2000.0
