"""Routing between the settlement asset (USDG / ETH) and a position.

Nothing on this chain launches denominated in USDG, so almost every entry crosses
an asset boundary. These tests pin where the extra hops appear and that the cost
of crossing is surfaced rather than folded away.
"""

from cambrian.runners import router as R
from cambrian.runners import venues as V

MM = 10 ** 18
STOCK = "0x00000000000000000000000000000000000000BB"


def _v(**kw) -> V.Venue:
    base = dict(kind=V.PONS_V2, token="0xtok", pool="0xcurve",
                quote_token=V.NATIVE_ETH, graduated=False,
                pricing_reserves=(10 * MM, 1000 * MM))
    base.update(kw)
    return V.Venue(**base)


def test_usdg_is_six_decimals_not_eighteen():
    # Verified on-chain. An 18-decimal assumption overstates a USDG amount by
    # 10^12 — it would size a position a trillion times too large, or revert.
    assert R.settlement_decimals(R.USDG) == 6
    assert R.to_units(25.0, R.USDG, price_usd=1.0) == 25_000_000
    assert R.settlement_decimals(R.NATIVE_ETH) == 18


def test_eth_amounts_still_scale_to_eighteen():
    assert R.to_units(1917.02, R.NATIVE_ETH, price_usd=1917.02) == 10 ** 18


def test_flash_routable_positions_are_a_single_hop_from_usdg():
    # Flash takes USDG as contra directly — live, $25 USDG into FRONG quoted
    # 0.18% impact.
    r = R.plan_route(_v(kind=V.FLAP), settlement_asset=R.USDG)
    assert r.hops == 1 and r.legs[0].kind == R.LEG_FLASH
    assert r.crosses_intermediate is False


def test_native_curve_settled_in_eth_needs_no_swap():
    r = R.plan_route(_v(quote_token=V.NATIVE_ETH), settlement_asset=V.NATIVE_ETH)
    assert r.hops == 1 and r.legs[0].kind == R.LEG_CURVE


def test_stock_paired_curve_from_usdg_takes_two_hops():
    # The common case: ~60% of Pons v2 is stock-paired, so a USDG balance has to
    # become the stock before the curve will take it.
    r = R.plan_route(_v(quote_token=STOCK), settlement_asset=R.USDG)
    assert r.hops == 2
    assert r.legs[0].kind == R.LEG_FLASH and r.legs[1].kind == R.LEG_CURVE
    assert r.legs[0].receive_asset == STOCK
    assert r.crosses_intermediate is True
    assert any("slippage is paid twice" in n for n in r.notes)


def test_two_hop_route_warns_about_intermediate_exposure():
    # A stock can move between the swap and the curve call, so the size that
    # arrives is not the size quoted.
    r = R.plan_route(_v(quote_token=STOCK), settlement_asset=R.USDG)
    assert any("exposed to" in n for n in r.notes)


def test_native_curve_from_usdg_also_needs_the_swap():
    r = R.plan_route(_v(quote_token=V.NATIVE_ETH), settlement_asset=R.USDG)
    assert r.hops == 2 and r.legs[0].receive_asset == V.NATIVE_ETH


# --- exits are not the entry reversed ---------------------------------------

def test_exit_from_a_stock_paired_curve_lands_in_the_stock_first():
    # A curve sale returns the CURVE's quote asset. Planning the exit as "the
    # entry backwards" would leave the desk holding equity it never chose.
    r = R.exit_route(_v(quote_token=STOCK), settlement_asset=R.USDG)
    assert r.hops == 2
    assert r.legs[0].kind == R.LEG_CURVE and r.legs[0].receive_asset == STOCK
    assert r.legs[1].receive_asset == R.USDG
    assert any("holds it until leg 2" in n for n in r.notes)


def test_exit_from_a_flash_venue_is_one_hop():
    r = R.exit_route(_v(kind=V.UNI_V2), settlement_asset=R.USDG)
    assert r.hops == 1 and r.legs[0].kind == R.LEG_FLASH


def test_graduated_curve_exits_through_flash():
    r = R.exit_route(_v(graduated=True), settlement_asset=R.USDG)
    assert r.legs[0].kind == R.LEG_FLASH


def test_describe_names_assets_and_flags_hops():
    r = R.plan_route(_v(quote_token=V.NATIVE_ETH), settlement_asset=R.USDG)
    s = R.describe(r)
    assert "USDG" in s and "ETH" in s and "2 hops" in s
