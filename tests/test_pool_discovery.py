from cambrian.runners.uniswap_v3 import decode_pool_created, price0_in_1
from cambrian.runners.feed import discover_new_pools
from cambrian.runners import blockscout as bs

WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"


def _topic(addr):
    return "0x" + addr[2:].rjust(64, "0")


def _pool_created_log(token0, token1, fee, pool):
    return {
        "topics": ["0xtopic0", _topic(token0), _topic(token1),
                   "0x" + format(fee, "064x")],
        "data": "0x" + format(0, "064x") + pool[2:].rjust(64, "0"),
    }


def test_decode_pool_created():
    d = decode_pool_created(_pool_created_log(TOKEN, WETH, 3000, POOL))
    assert d["token0"].lower() == TOKEN.lower()
    assert d["token1"].lower() == WETH.lower()
    assert d["fee"] == 3000
    assert d["pool"].lower() == POOL.lower()


def test_price0_in_1_at_parity():
    # sqrtPriceX96 = 2**96 -> price 1.0 when decimals match
    assert abs(price0_in_1(1 << 96, 18, 18) - 1.0) < 1e-9


class _FakeClient:
    def __init__(self, logs):
        self._logs = logs
    def get_logs(self, **kw):
        return self._logs


def test_discover_keeps_only_weth_pairs():
    logs = [
        _pool_created_log(TOKEN, WETH, 3000, POOL),               # WETH pair -> keep
        _pool_created_log(TOKEN, "0x9999999999999999999999999999999999999999",
                          500, "0x3333333333333333333333333333333333333333"),  # no WETH -> drop
    ]
    out = discover_new_pools(_FakeClient(logs), v3_factory="0xfac", weth=WETH,
                             from_block="0x0")
    assert len(out) == 1
    assert out[0]["token"].lower() == TOKEN.lower()
    # TOKEN is token0, WETH is token1 -> weth is NOT token0
    assert out[0]["weth_is_token0"] is False


def test_top_holder_pct_and_count():
    token_json = {"total_supply": "1000000", "holders": "42"}
    holders_json = {"items": [{"value": "250000"}, {"value": "100000"}]}
    assert bs.holder_count(token_json) == 42
    assert abs(bs.top_holder_pct(token_json, holders_json) - 0.25) < 1e-9


def test_top_holder_pct_none_without_supply():
    assert bs.top_holder_pct({"total_supply": "0"}, {"items": []}) is None
