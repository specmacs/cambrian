"""The unified venue layer and MC-based sizing.

The point of this layer is that a token's pad should stop mattering: one call
prices it, one call costs it, one call decides whether to touch it, and one call
sizes it — whether it lives on a v3 pool, a v4 pool, a real V2 pair, or a bonding
curve quoting against reserves it does not actually hold.
"""

from cambrian.runners import sizing as S
from cambrian.runners import venues as V

MM = 10 ** 18


def _curve(**kw) -> V.Venue:
    base = dict(kind=V.PONS_V2, token="0xtok", pool="0xcurve",
                quote_token=V.NATIVE_ETH, quote_decimals=18, token_decimals=18,
                pricing_reserves=(10 * MM, 1000 * MM), real_backing_quote=0,
                fee_bps=100, tax_bps=0, total_supply=10**27, graduated=False,
                sellable_tokens=10**30)
    base.update(kw)
    return V.Venue(**base)


# --- pricing is venue-agnostic ----------------------------------------------

def test_price_and_market_cap_are_fdv_on_a_fully_minted_supply():
    v = _curve()
    assert V.price_quote_per_token(v) == 0.01          # 10 quote / 1000 tokens
    assert V.market_cap_quote(v) == 0.01 * 1e9          # 1e27 wei-supply = 1e9 tokens
    assert V.market_cap_usd(v, 3000.0) == 0.01 * 1e9 * 3000


def test_market_cap_handles_a_six_decimal_quote_asset():
    # A live Pons v2 curve paired against a 6-decimal asset; assuming 18 would
    # misprice market cap by 10^12 and size the ticket accordingly.
    v = _curve(quote_decimals=6, pricing_reserves=(10 * 10**6, 1000 * MM))
    assert V.price_quote_per_token(v) == 0.01


def test_unknown_reserves_price_to_none_not_zero():
    assert V.price_quote_per_token(_curve(pricing_reserves=None)) is None
    assert V.market_cap_usd(_curve(pricing_reserves=None), 3000.0) is None


# --- costs ------------------------------------------------------------------

def test_buy_takes_fees_off_input_and_sell_off_output():
    v = _curve(fee_bps=100, tax_bps=0)
    got = V.quote_buy(v, MM)
    net = MM - MM // 100
    assert got == (net * 1000 * MM) // (10 * MM + net)
    tokens = 10 * MM
    gross = (tokens * 10 * MM) // (1000 * MM + tokens)
    assert V.quote_sell(v, tokens) == gross - gross // 100


def test_round_trip_separates_tax_from_slippage():
    # The distinction that matters: tax is fixed per token and screened for,
    # slippage scales with size and is sized for. A combined number hides which.
    taxed = _curve(tax_bps=600)
    rt = V.round_trip(taxed, MM // 10)
    assert rt["fee_tax_bps"] == 2 * 700
    assert rt["slippage_bps"] < rt["total_loss_bps"]
    clean = V.round_trip(_curve(fee_bps=0, tax_bps=0), MM // 10)
    assert clean["fee_tax_bps"] == 0
    assert clean["slippage_bps"] > 0          # slippage survives with zero fees


def test_buy_is_clamped_to_sellable_tokens():
    assert V.quote_buy(_curve(sellable_tokens=1000), MM) == 1000


# --- the gate ---------------------------------------------------------------

def test_gate_blocks_tax_above_the_limit():
    assert V.tradeable(_curve(tax_bps=1000))["ok"] is False
    assert V.tradeable(_curve(tax_bps=300))["ok"] is True


def test_gate_fails_closed_without_a_price():
    r = V.tradeable(_curve(pricing_reserves=None))
    assert r["ok"] is False and "no price" in r["reasons"]


def test_gate_can_require_real_backing():
    # flap's pair shell reports reserves it does not hold; requiring backing is
    # how a caller refuses to price an exit against assets that are not there.
    shell = _curve(kind=V.FLAP, real_backing_quote=None)
    assert V.tradeable(shell, require_backing=True)["ok"] is False
    assert V.tradeable(shell)["ok"] is True        # not required by default
    real = _curve(real_backing_quote=5 * MM)
    assert V.tradeable(real, require_backing=True)["ok"] is True


def test_curve_venues_are_flagged_as_pricing_off_virtual_reserves():
    assert _curve(kind=V.PONS_V2).prices_off_virtual_reserves is True
    assert _curve(kind=V.FLAP).prices_off_virtual_reserves is True
    assert _curve(kind=V.UNI_V2).prices_off_virtual_reserves is False


# --- sizing -----------------------------------------------------------------

def test_standard_ticket_is_a_fraction_of_market_cap():
    assert S.target_size_usd(20_000, bankroll_usd=10_000, bps_of_mc=50,
                             risk_bps=10_000, max_usd=1e9) == 100.0


def test_bankroll_cap_overrides_a_large_market_cap():
    # A $10m runner must not produce a $50k ticket out of a $1k account.
    got = S.target_size_usd(10_000_000, bankroll_usd=1_000, bps_of_mc=50,
                            risk_bps=500, max_usd=1e9)
    assert got == 50.0


def test_tiny_market_caps_fall_below_the_minimum_and_size_to_zero():
    assert S.target_size_usd(100, bankroll_usd=1000, min_usd=10) == 0.0


def test_unknown_market_cap_sizes_to_none():
    assert S.target_size_usd(None) is None


def test_slippage_cap_shrinks_the_ticket_on_a_thin_curve():
    thin = _curve(pricing_reserves=(MM, 1000 * MM))    # 1 quote of depth
    want = MM  # 100% of depth — enormous
    capped = S.cap_for_slippage(thin, want, max_slippage_bps=300)
    assert 0 < capped < want
    assert (S.slippage_bps_for(thin, capped) or 0) <= 300


def test_slippage_cap_leaves_a_deep_venue_alone():
    deep = _curve(pricing_reserves=(10_000 * MM, 10**9 * MM))
    want = MM // 100
    assert S.cap_for_slippage(deep, want, max_slippage_bps=300) == want


def test_plan_entry_produces_an_actionable_ticket():
    v = _curve(pricing_reserves=(10_000 * MM, 10**9 * MM), tax_bps=0, fee_bps=100)
    plan = S.plan_entry(v, quote_price_usd=3000.0, bankroll_usd=1000)
    assert plan["ok"] is True
    assert plan["size_usd"] > 0 and plan["quote_in"] > 0
    assert plan["market_cap_usd"] is not None
    assert plan["round_trip"]["returned_pct"] > 0


def test_plan_entry_refuses_a_taxed_token_and_says_why():
    plan = S.plan_entry(_curve(tax_bps=1000), quote_price_usd=3000.0)
    assert plan["ok"] is False
    assert any("tax" in r for r in plan["reasons"])


def test_plan_entry_explains_every_refusal():
    # "No trade" without a reason is indistinguishable from a broken feed, which
    # is how pons and flap sat unwatched.
    for v in (_curve(pricing_reserves=None), _curve(tax_bps=5000)):
        plan = S.plan_entry(v, quote_price_usd=3000.0)
        assert plan["ok"] is False and plan["reasons"]
