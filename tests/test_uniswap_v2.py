"""Uniswap V2 decoding + constant-product math, and the flap tax gate.

V2 matters because flap — the chain's highest-volume launch source — trades there
and nowhere else. The desk previously scanned only v3/v4 and therefore reported
flap tokens as having no pool at all.
"""

from cambrian.runners import uniswap_v2 as v2
from cambrian.runners.flap_tax import (MAX_TAX_BPS, read_tax_bps, tax_ok,
                                       worst_tax_bps)

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def _w(n: int) -> str:
    return f"{n:064x}"


def _keccak(data: bytes) -> bytes:
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "examples" / "rh_codehash.py"
    text = src.read_text()
    ns: dict = {}
    exec(text[text.index("_M = (1 << 64)"):text.index("def rpc(")], ns)
    return ns["kec"](data)


def test_topic0s_are_keccak_of_the_v2_signatures():
    assert v2.PAIRCREATED_TOPIC0 == "0x" + _keccak(
        b"PairCreated(address,address,address,uint256)").hex()
    assert v2.SWAP_TOPIC0 == "0x" + _keccak(
        b"Swap(address,uint256,uint256,uint256,uint256,address)").hex()
    assert v2.SYNC_TOPIC0 == "0x" + _keccak(b"Sync(uint112,uint112)").hex()


def test_v2_swap_folds_to_the_v3_pool_perspective_sign():
    # A BUY: WETH (token0) goes INTO the pool, tokens come out. v3 convention says
    # that is a positive amount0 — so aggregate_swaps counts it as a buy with
    # invert=False, exactly like v3.
    log = {"data": "0x" + _w(10**18) + _w(0) + _w(0) + _w(5 * 10**18)}
    d = v2.decode_v2_swap(log)
    assert d["amount0"] == 10**18
    assert d["amount1"] == -5 * 10**18


def test_v2_sell_is_negative_on_the_weth_leg():
    log = {"data": "0x" + _w(0) + _w(5 * 10**18) + _w(10**18) + _w(0)}
    d = v2.decode_v2_swap(log)
    assert d["amount0"] == -10**18


def test_aggregate_swaps_reads_v2_buys_without_inversion():
    # Guards the sign contract between the two modules: if decode_v2_swap ever
    # stopped folding to the pool perspective, every V2 buy would read as a sell —
    # the exact bug that hit v4.
    buy = v2.decode_v2_swap({"data": "0x" + _w(10**18) + _w(0) + _w(0) + _w(7)})
    out = v2.aggregate_swaps([buy], weth_is_token0=True, weth_price_usd=3000.0)
    assert out["buys"] == 1 and out["sells"] == 0
    assert out["net_usd"] == 3000.0


def test_pair_created_decodes_token_and_pair():
    log = {"topics": ["0x" + "00" * 32, "0x" + _w(int(WETH, 16)),
                      "0x" + _w(0xabc)],
           "data": "0x" + _w(0xdeadbeef) + _w(42)}
    d = v2.decode_pair_created(log)
    assert d["token0"] == WETH
    assert d["pair"].endswith("deadbeef")
    assert d["index"] == 42


def test_amount_out_matches_the_constant_product_invariant():
    # 1 WETH into a 10 WETH / 1000 token pool, 0.3% fee.
    r_in, r_out = 10 * 10**18, 1000 * 10**18
    got = v2.amount_out(10**18, r_in, r_out)
    net = 10**18 * 9970
    expected = (net * r_out) // (r_in * 10000 + net)
    assert got == expected
    # output must stay strictly under the naive no-slippage share
    assert got < 100 * 10**18


def test_amount_out_is_zero_on_empty_or_invalid_reserves():
    assert v2.amount_out(10**18, 0, 10**18) == 0
    assert v2.amount_out(0, 10**18, 10**18) == 0
    assert v2.amount_out(10**18, 10**18, 0) == 0


def test_liquidity_is_exact_quote_reserve_doubled():
    # 1.9190 WETH is what every sampled flap pair is seeded with.
    seed = 1_919_000_000_000_000_000
    usd = v2.liquidity_usd_from_reserves((seed, 10**24), quote_is_token0=True,
                                         quote_price_usd=3000.0)
    assert abs(usd - 1.919 * 3000 * 2) < 1e-6


def test_unreadable_reserves_stay_none_not_zero():
    # None means UNKNOWN so the scorer fails closed; 0 would read as "no liquidity",
    # which is a different (and answerable) claim.
    class C:
        def eth_call(self, to, data):
            return "0x"
    assert v2.read_reserves(C(), "0xpair") is None
    assert v2.liquidity_usd_from_reserves(None, quote_is_token0=True,
                                          quote_price_usd=3000.0) is None


def test_sell_value_applies_the_tax_haircut():
    reserves = (10 * 10**18, 1000 * 10**18)
    clean = v2.sell_value_quote(10**18, reserves, quote_is_token0=True)
    taxed = v2.sell_value_quote(10**18, reserves, quote_is_token0=True, tax_bps=1000)
    assert taxed < clean          # a 10% tax must show up as less value back
    assert taxed > 0


# --- the owner's 3% tax rule -------------------------------------------------

class _TaxClient:
    def __init__(self, buy=None, sell=None, fail=False):
        self.buy, self.sell, self.fail = buy, sell, fail

    def eth_call(self, to, data):
        if self.fail:
            raise RuntimeError("reverted")
        from cambrian.runners.flap_tax import BUY_TAX_SELECTOR
        v = self.buy if data == BUY_TAX_SELECTOR else self.sell
        return None if v is None else hex(v)


def test_reads_both_tax_legs_in_basis_points():
    t = read_tax_bps(_TaxClient(buy=300, sell=300), "0xtok")
    assert t == {"buy": 300, "sell": 300}
    assert worst_tax_bps(t) == 300


def test_ten_percent_tax_is_rejected():
    # 62% of live flap launches sit here — this is the common case, not the edge.
    assert tax_ok({"buy": 1000, "sell": 1000}) is False


def test_three_percent_is_allowed_and_anything_above_is_not():
    assert tax_ok({"buy": MAX_TAX_BPS, "sell": MAX_TAX_BPS}) is True
    assert tax_ok({"buy": MAX_TAX_BPS + 1, "sell": MAX_TAX_BPS + 1}) is False
    # arbitrary real rates seen on-chain, not the documented 1/3/5/10 tiers
    assert tax_ok({"buy": 730, "sell": 730}) is False
    assert tax_ok({"buy": 130, "sell": 130}) is True


def test_an_asymmetric_sell_tax_is_caught():
    # buy looks cheap, exit does not — the leg that matters is the worse one.
    assert tax_ok({"buy": 100, "sell": 1000}) is False


def test_plain_erc20_without_a_tax_function_is_treated_as_untaxed():
    # pons and pools.trade tokens have no tax function; rejecting them would
    # disable the rest of the chain.
    t = read_tax_bps(_TaxClient(fail=True), "0xtok")
    assert t == {"buy": None, "sell": None}
    assert tax_ok(t) is True


def test_unknown_tax_can_fail_closed_for_flap_tokens():
    t = {"buy": None, "sell": None}
    assert tax_ok(t, allow_unknown=False) is False


def test_safety_agent_hard_zeroes_a_taxed_token_even_from_a_verified_pad():
    # The gate must outrank pad verification: a verified, sellable flap token can
    # still hand back 10% on exit, and that is a certain loss rather than a risk.
    from cambrian.runners.agents import agent_safety, confluence
    score, reason = agent_safety(True, True, tax_bps=1000)
    assert score == 0.0 and "TAX" in reason
    verdict = confluence({"flow": 1.0, "smart_money": 1.0, "sniper": 1.0},
                         safety=score)
    assert verdict["gated"] is True and verdict["tier"] == "blocked"


def test_safety_agent_unchanged_when_tax_is_within_limit():
    from cambrian.runners.agents import agent_safety
    assert agent_safety(True, True, tax_bps=300)[0] == 1.0
    assert agent_safety(True, True)[0] == 1.0     # back-compat: tax optional
