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


# --- the sweep --------------------------------------------------------------

class _FakeClient:
    """Replays a fixed log set, and errors on any range wider than the cap."""
    def __init__(self, per_call=None, cap=1500, fail_all=False):
        self.per_call = per_call or {}
        self.cap, self.fail_all, self.calls = cap, fail_all, []

    def get_logs(self, *, address=None, topics=None, from_block=None, to_block=None):
        lo, hi = int(from_block, 16), int(to_block, 16)
        self.calls.append((lo, hi))
        if hi - lo > self.cap or self.fail_all:
            raise RuntimeError("requested logs from too many blocks")
        return self.per_call.get((address or "").lower(), [])

    def eth_call(self, to, data):
        return "0x"


def test_chunked_logs_splits_to_respect_the_node_cap():
    # A single 10k-block request errors on this RPC; the scanner must never issue
    # one, or it silently reports zero launches.
    from cambrian.runners.scanner import chunked_logs
    c = _FakeClient(cap=1500)
    logs, failed = chunked_logs(c, address="0xa", topics=["0xt"],
                                from_block=0, to_block=10_000, span=1500)
    assert failed == 0
    assert all(hi - lo <= 1500 for lo, hi in c.calls)
    assert len(c.calls) == 7


def test_chunked_logs_reports_failures_instead_of_hiding_them():
    # A swallowed failure is indistinguishable from a quiet market.
    from cambrian.runners.scanner import chunked_logs
    c = _FakeClient(fail_all=True)
    logs, failed = chunked_logs(c, address="0xa", topics=["0xt"],
                                from_block=0, to_block=3000, span=1500)
    # 0-1500 and 1501-3000: two chunks, both failing, neither hidden
    assert logs == [] and failed == 2


def test_sweep_surfaces_incomplete_scans_in_its_summary():
    from cambrian.runners.scanner import format_sweep
    txt = format_sweep({"rows": [], "scanned_blocks": 3000, "found": 0,
                        "tradeable": 0, "failed_chunks": 2})
    assert "CHUNKS FAILED" in txt


def test_sweep_keeps_blocked_rows_with_their_reasons():
    # Showing only passes makes a broken gate look like a quiet market.
    from cambrian.runners.scanner import format_sweep
    txt = format_sweep({"rows": [{"pad": "flap", "token": "0xdead", "ok": False,
                                  "reasons": ["tax 10% > 3%"], "market_cap_usd": 5200,
                                  "size_usd": None, "tax_bps": 1000}],
                        "scanned_blocks": 1500, "found": 1, "tradeable": 0,
                        "failed_chunks": 0})
    assert "tax 10% > 3%" in txt and "flap" in txt


# --- stock-token quote assets -----------------------------------------------

def _stub_registry(monkeypatch, mapping):
    from cambrian.runners import stock_tokens as ST
    ST._registry = {k.lower(): v for k, v in mapping.items()}
    ST._registry_at = 1e18          # far future: never refetch during a test
    return ST


def test_stock_token_identity_is_registry_membership_not_ticker(monkeypatch):
    # RH's docs: a token with a matching ticker but a different address is NOT a
    # stock token. An impostor "AAPL" must not inherit the looser tax ceiling.
    ST = _stub_registry(monkeypatch, {
        "0xaaa": {"symbol": "AAPL", "name": "Apple", "multiplier": 1.0}})
    assert ST.is_stock_token("0xAAA") is True
    assert ST.is_stock_token("0xbbb") is False


def test_stock_paired_launches_get_the_higher_tax_ceiling(monkeypatch):
    from cambrian.runners import config as rcfg
    from cambrian.runners.flap_tax import MAX_TAX_BPS
    _stub_registry(monkeypatch, {"0xspy": {"symbol": "SPY", "name": "S&P",
                                           "multiplier": 1.0}})
    stock = _curve(quote_token="0xspy", tax_bps=500)
    eth = _curve(quote_token=V.NATIVE_ETH, tax_bps=500)
    assert V.max_tax_for(stock) == rcfg.STOCK_PAIRED_MAX_TAX_BPS
    assert V.max_tax_for(eth) == MAX_TAX_BPS
    # 5% is exactly the observed stock-pair rate: apeable there, blocked elsewhere
    assert V.tradeable(stock)["ok"] is True
    assert V.tradeable(eth)["ok"] is False


def test_stock_ceiling_does_not_leak_to_flap(monkeypatch):
    # A blanket 5% would wave through half of flap, which is the flow the 3% rule
    # exists to keep out.
    _stub_registry(monkeypatch, {"0xspy": {"symbol": "SPY", "multiplier": 1.0}})
    flap = _curve(kind=V.FLAP, quote_token="0xweth", tax_bps=500)
    assert V.tradeable(flap)["ok"] is False


def test_quote_price_resolves_each_asset_class(monkeypatch):
    ST = _stub_registry(monkeypatch, {"0xspy": {"symbol": "SPY", "multiplier": 1.0}})
    monkeypatch.setattr(ST, "usd_price", lambda a, **k: 773.34)
    assert ST.quote_price_usd(V.NATIVE_ETH, weth_usd=3000.0) == 3000.0
    assert ST.quote_price_usd("0xWETH", weth_usd=3000.0, weth="0xweth") == 3000.0
    assert ST.quote_price_usd("0xUSDG", weth_usd=3000.0, usdg="0xusdg") == 1.0
    assert ST.quote_price_usd("0xspy", weth_usd=3000.0) == 773.34


def test_unknown_quote_asset_blocks_rather_than_defaulting_to_eth(monkeypatch):
    # Sizing is a fraction of market cap, so silently substituting the ETH price
    # for an unknown quote asset would mis-size every ticket against it.
    ST = _stub_registry(monkeypatch, {})
    assert ST.quote_price_usd("0xmystery", weth_usd=3000.0) is None


def test_corporate_action_multiplier_is_applied(monkeypatch):
    # A live 4.0 multiplier (CRWD): one token represents four post-split shares,
    # so ignoring it prices the token at a quarter of its worth.
    from cambrian.runners import stock_tokens as ST
    _stub_registry(monkeypatch, {"0xcrwd": {"symbol": "CRWD", "multiplier": 4.0}})
    ST._prices["CRWD"] = (100.0, 1e18)      # cached mid, far-future timestamp
    assert ST.usd_price("0xcrwd", now=1e18) == 400.0


def test_stock_price_uses_the_mid_not_one_side(monkeypatch):
    # GME quoted 19.08/19.99 live; taking a side would bias every market cap.
    from cambrian.runners import stock_tokens as ST
    _stub_registry(monkeypatch, {"0xgme": {"symbol": "GME", "multiplier": 1.0}})
    ST._prices.pop("GME", None)
    monkeypatch.setattr(ST, "_get", lambda url, timeout=10.0: {
        "quotes": [{"bid": "19.08", "ask": "19.99"}]})
    assert ST.usd_price("0xgme") == (19.08 + 19.99) / 2


# --- Uniswap V3 venues (Pons v1) --------------------------------------------

def _v3(**kw) -> V.Venue:
    base = dict(kind=V.PONS_V1, token="0xtok", pool="0xpool",
                quote_token="0xweth", quote_decimals=18, token_decimals=18,
                pricing_reserves=None, total_supply=10**27,
                spot_price_quote_per_token=2.5e-8)
    base.update(kw)
    return V.Venue(**base)


def test_v3_venues_price_from_spot_not_reserves():
    # A concentrated pool has no constant-product reserves; faking x*y=k on one
    # misprices depth in both directions. Spot is exact and is all MC needs.
    v = _v3()
    assert V.price_quote_per_token(v) == 2.5e-8
    assert V.market_cap_usd(v, 2000.0) == 2.5e-8 * 1e9 * 2000.0


def test_v3_venue_passes_the_gate_on_spot_price_alone():
    # The live bug: 11 of 11 Pons v1 launches failed as "no price" because the V2
    # reader returned nothing on a V3 pool. The gate was fine; the resolver wasn't.
    assert V.tradeable(_v3())["ok"] is True
    assert V.tradeable(_v3(spot_price_quote_per_token=None))["ok"] is False


def test_v3_venue_has_no_local_round_trip_quote():
    v = _v3()
    assert V.quote_buy(v, 10**18) is None
    assert V.quote_sell(v, 10**18) is None


def test_v3_plan_sizes_off_market_cap_and_defers_slippage_to_flash():
    plan = S.plan_entry(_v3(), quote_price_usd=2000.0, bankroll_usd=1000)
    assert plan["ok"] is True
    assert plan["size_usd"] > 0 and plan["quote_in"] > 0
    assert plan["slippage_priced_by"] == "flash-at-execution"
    assert plan["round_trip"] is None      # honest: we did not compute one


def test_fee_farm_share_counts_tokens_at_the_maximum_tax():
    # Owner's read, confirmed live: 36 of 53 flap tokens sat at exactly 10.0%.
    # A real launch does not pick the cap — it makes the token unsellable at a
    # profit — so this ratio is the fee-farm tell.
    from cambrian.runners.scanner import fee_farm_share
    result = {"rows": [{"tax_bps": 1000}, {"tax_bps": 1000}, {"tax_bps": 230},
                       {"tax_bps": None}]}
    assert fee_farm_share(result) == (2, 3)


def test_blocked_rows_are_summarised_not_dropped():
    # They must not vanish (a hidden gate looks like a quiet market) but at ~68%
    # of flap at max tax, listing every one buries what is actionable.
    from cambrian.runners.scanner import format_sweep
    rows = [{"pad": "flap", "token": "0x%040x" % i, "ok": False,
             "reasons": ["tax 10% > 3%"], "tax_bps": 1000,
             "market_cap_usd": 5000, "size_usd": None} for i in range(20)]
    txt = format_sweep({"rows": rows, "scanned_blocks": 1500, "found": 20,
                        "tradeable": 0, "failed_chunks": 0}, show_blocked=3)
    assert "...17 more blocked" in txt
    assert "tax x20" in txt
    assert "fee farms, not launches" in txt
