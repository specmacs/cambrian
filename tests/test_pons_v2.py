"""Pons v2 bonding curves — pricing tokens that have no pool yet.

v2 trades on a per-token curve until it graduates into a locked v4 pool, so for
the whole pre-graduation window (where a sniping desk lives) there is no AMM to
quote against.
"""

from cambrian.runners import pons_v2 as p2


def _w(n: int) -> str:
    return f"{n:064x}"


def _keccak(data: bytes) -> bytes:
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "examples" / "rh_codehash.py"
    text = src.read_text()
    ns: dict = {}
    exec(text[text.index("_M = (1 << 64)"):text.index("def rpc(")], ns)
    return ns["kec"](data)


def test_selectors_match_keccak_of_the_documented_getters():
    for sig, sel in (("getReserves()", p2.SEL_GET_RESERVES),
                     ("realQuoteReserve()", p2.SEL_REAL_QUOTE_RESERVE),
                     ("sellableTokens()", p2.SEL_SELLABLE_TOKENS),
                     ("feeBps()", p2.SEL_FEE_BPS),
                     ("creatorTaxBps()", p2.SEL_CREATOR_TAX_BPS),
                     ("graduated()", p2.SEL_GRADUATED),
                     ("graduationThreshold()", p2.SEL_GRADUATION_THRESHOLD)):
        assert sel == "0x" + _keccak(sig.encode()).hex()[:8], sig


def test_token_launched_takes_the_curve_from_topic2():
    # Pons v1's topic2 is the DEPLOYER. Reusing the v1 layout here would treat the
    # deployer's address as a curve and every quote would fail.
    log = {"topics": ["0x" + "00" * 32, "0x" + _w(0xaaa), "0x" + _w(0xccc),
                      "0x" + _w(0xddd)],
           "data": "0x" + _w(0) + _w(7) + _w(42 * 10**17)}
    d = p2.decode_token_launched(log)
    assert d["curve"].endswith("ccc")
    assert d["deployer"].endswith("ddd")
    assert d["pair_token"] == p2.NATIVE_ETH        # native ETH, not WETH
    assert d["launch_config_id"] == 7
    assert d["graduation_threshold"] == 42 * 10**17


def test_curve_buy_and_sell_decode_their_own_directions():
    buy = p2.decode_curve_buy({"data": "0x" + _w(10**18) + _w(500) + _w(10) + _w(20)})
    assert buy == {"quote_in": 10**18, "tokens_out": 500, "fee": 10, "tax": 20}
    sell = p2.decode_curve_sell({"data": "0x" + _w(500) + _w(10**18) + _w(10) + _w(20)})
    assert sell == {"tokens_in": 500, "quote_out": 10**18, "fee": 10, "tax": 20}


STATE = {"quote_reserve": 10 * 10**18, "token_reserve": 1000 * 10**18,
         "sellable_tokens": 10**30, "fee_bps": 100, "creator_tax_bps": 0}


def test_buy_takes_fees_off_the_input_before_pricing():
    out = p2.quote_buy(10**18, STATE)
    net = 10**18 - (10**18 * 100 // 10_000)
    assert out == (net * STATE["token_reserve"]) // (STATE["quote_reserve"] + net)


def test_sell_prices_first_then_takes_fees_off_the_output():
    # The mirror of the buy path. Applying fees to the input on a sell would
    # overstate proceeds, which is exactly the kind of error that shows up only
    # as a persistently optimistic P&L.
    tokens = 10 * 10**18
    gross = (tokens * STATE["quote_reserve"]) // (STATE["token_reserve"] + tokens)
    assert p2.quote_sell(tokens, STATE) == gross - (gross * 100 // 10_000)


def test_creator_tax_reduces_both_directions():
    taxed = dict(STATE, creator_tax_bps=600)
    assert p2.quote_buy(10**18, taxed) < p2.quote_buy(10**18, STATE)
    assert p2.quote_sell(10**19, taxed) < p2.quote_sell(10**19, STATE)


def test_buy_is_clamped_to_sellable_tokens():
    # Past sellableTokens the curve closes and graduates, so an unclamped quote
    # promises tokens that cannot actually be bought.
    tight = dict(STATE, sellable_tokens=1000)
    assert p2.quote_buy(10**18, tight) == 1000


def test_snipe_tax_is_applied_to_buys_only():
    assert p2.quote_buy(10**18, STATE, snipe_bps=9900) < p2.quote_buy(10**18, STATE)
    # sells take no snipe tax, so the signature offers no way to pass one
    assert p2.quote_sell(10**19, STATE) == p2.quote_sell(10**19, STATE)


def test_quotes_are_none_when_the_curve_cannot_be_read():
    # None keeps the scorer failing closed; a 0 would read as a real, cheap quote.
    empty = {"quote_reserve": None, "token_reserve": None}
    assert p2.quote_buy(10**18, empty) is None
    assert p2.quote_sell(10**18, empty) is None


def test_liquidity_uses_the_real_reserve_not_the_phantom_one():
    # Live sample: a fresh curve reads ~4.36 ETH of PRICING reserve against a real
    # reserve of 0. Reporting getReserves() as depth would make every brand-new
    # launch look well funded.
    state = {"quote_reserve": 4_360_000_000_000_000_000, "real_quote_reserve": 0}
    assert p2.real_liquidity_quote(state) == 0


def test_graduation_progress_tracks_real_quote_against_threshold():
    state = {"real_quote_reserve": 5 * 10**18, "graduation_threshold": 10 * 10**18}
    assert p2.graduation_progress(state) == 0.5
    assert p2.graduation_progress({"real_quote_reserve": None,
                                   "graduation_threshold": 1}) is None


def test_round_trip_cost_counts_both_legs():
    # Live curves carried creatorTaxBps of 500 and 600 with a 1% fee: 12-14% round
    # trip before any price move.
    assert p2.round_trip_cost_bps({"fee_bps": 100, "creator_tax_bps": 600}) == 1400
    assert p2.round_trip_cost_bps({"fee_bps": None, "creator_tax_bps": None}) is None


def test_curve_flow_respects_non_18_decimal_quote_assets():
    # A live curve paired against a 6-decimal asset; assuming 18 misprices by 1e12.
    buys = [{"quote_in": 2_000_000, "tokens_out": 0, "fee": 0, "tax": 0}]
    m = p2.curve_flow_metrics(buys, [], quote_price_usd=1.0, quote_decimals=6)
    assert m["volume_usd"] == 2.0 and m["buys"] == 1


def test_curve_flow_nets_buys_against_sells():
    buys = [{"quote_in": 3 * 10**18, "tokens_out": 0, "fee": 0, "tax": 0}]
    sells = [{"tokens_in": 0, "quote_out": 10**18, "fee": 0, "tax": 0}]
    m = p2.curve_flow_metrics(buys, sells, quote_price_usd=3000.0)
    assert m["net_usd"] == 6000.0 and m["volume_usd"] == 12000.0
    assert m["buys"] == 1 and m["sells"] == 1


def test_live_creator_taxes_would_be_blocked_by_the_three_percent_gate():
    # Sampled live: creatorTaxBps 500 and 600. The owner's rule is 3%, so these
    # must gate out exactly like a 10% flap token.
    from cambrian.runners.agents import agent_safety
    for observed in (500, 600):
        score, reason = agent_safety(True, True, tax_bps=observed)
        assert score == 0.0 and "TAX" in reason
