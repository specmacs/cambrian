from cambrian.runners.uniswap_v4 import (NATIVE_ETH, decode_initialize,
                                         decode_v4_swap)
from cambrian.runners.uniswap_v3 import aggregate_swaps
from cambrian.runners.feed import discover_new_pools_v4

WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
TOKEN = "0x1111111111111111111111111111111111111111"
HOOK = "0x4444444444444444444444444444444444444444"
POOL_ID = "0x" + "ab" * 32


def _w(v: int) -> str:
    return format(v & ((1 << 256) - 1), "064x")


def _addr_word(a: str) -> str:
    return a[2:].rjust(64, "0")


def _init_log(cur0, cur1, fee, hook):
    data = "0x" + _w(fee) + _w(0) + _addr_word(hook) + _w(1 << 96) + _w(0)
    return {"topics": ["0xt0", POOL_ID, "0x" + _addr_word(cur0),
                       "0x" + _addr_word(cur1)], "data": data,
            "blockNumber": "0x64"}


def test_decode_initialize_captures_hook_and_currencies():
    d = decode_initialize(_init_log(TOKEN, WETH, 3000, HOOK))
    assert d["currency0"].lower() == TOKEN.lower()
    assert d["currency1"].lower() == WETH.lower()
    assert d["hooks"].lower() == HOOK.lower()
    assert d["fee"] == 3000
    assert d["pool_id"] == POOL_ID


def test_decode_v4_swap_and_aggregate():
    # v4 is SWAPPER-perspective: a buyer PAYS WETH, so a positive WETH amount here
    # means the swapper RECEIVED WETH = they sold. Without invert it'd misread as a
    # buy (the v3 rule); with invert=True the sign is corrected.
    data = "0x" + _w(-500) + _w(2 * 10**18) + _w(0) + _w(0) + _w(0) + _w(3000)
    d = decode_v4_swap({"data": data})
    assert d["amount0"] == -500 and d["amount1"] == 2 * 10**18
    # v3 rule (no invert) would wrongly call this a buy:
    naive = aggregate_swaps([d], weth_is_token0=False, weth_price_usd=3000.0)
    assert naive["buys"] == 1
    # v4 rule (invert=True): positive WETH to the swapper = a sell. Volume unchanged.
    out = aggregate_swaps([d], weth_is_token0=False, weth_price_usd=3000.0, invert=True)
    assert out["sells"] == 1 and out["buys"] == 0
    assert out["volume_usd"] == 2 * 3000.0


class _Fake:
    def __init__(self, logs):
        self._logs = logs
    def get_logs(self, **kw):
        return self._logs


def test_discover_v4_keeps_weth_and_native_pairs():
    logs = [
        _init_log(TOKEN, WETH, 3000, HOOK),          # WETH pair -> keep
        _init_log(TOKEN, NATIVE_ETH, 500, "0x" + "0" * 40),  # native ETH -> keep
        _init_log(TOKEN, "0x9999999999999999999999999999999999999999", 500, HOOK),  # drop
    ]
    out = discover_new_pools_v4(_Fake(logs), pool_manager="0xpm", weth=WETH,
                                from_block="0x0")
    assert len(out) == 2
    assert out[0]["hooks"].lower() == HOOK.lower()
    assert out[1]["quote"] == "ETH"
    assert all(o["token"].lower() == TOKEN.lower() for o in out)
