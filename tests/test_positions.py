"""Marking positions honestly, and getting out of them.

The two invariants that matter most are corrections to how this desk used to
work: mark at the real exit price, and never let a failed sell quote preserve a
stale mark.
"""

import time

from cambrian.runners import positions as P
from cambrian.runners import venues as V

MM = 10 ** 18


def _v(**kw) -> V.Venue:
    base = dict(kind=V.PONS_V2, token="0xtok", pool="0xcurve",
                quote_token=V.NATIVE_ETH, quote_decimals=18, token_decimals=18,
                pricing_reserves=(10 * MM, 1000 * MM), fee_bps=100, tax_bps=0,
                total_supply=10**27, graduated=False, sellable_tokens=10**30)
    base.update(kw)
    return V.Venue(**base)


def _pos(**kw) -> P.Position:
    base = dict(token="0xtok", venue_kind=V.PONS_V2, pool="0xcurve",
                tokens=10 * MM, cost_usd=100.0, opened_at=time.time(),
                peak_usd=100.0)
    base.update(kw)
    return P.Position(**base)


# --- marking ----------------------------------------------------------------

def test_mark_uses_the_real_sell_quote_not_the_mid_price():
    # Tax and slippage both land on the exit, so a mid-price mark overstates the
    # position — the number that makes a desk feel profitable while it bleeds.
    v = _v(tax_bps=0)
    taxed = _v(tax_bps=1000)
    clean_val, _ = P.exit_value_usd(v, 10 * MM, quote_price_usd=2000.0)
    taxed_val, _ = P.exit_value_usd(taxed, 10 * MM, quote_price_usd=2000.0)
    assert taxed_val < clean_val


def test_mark_includes_slippage_of_the_actual_size():
    v = _v()
    small, exact = P.exit_value_usd(v, MM, quote_price_usd=2000.0)
    big, _ = P.exit_value_usd(v, 100 * MM, quote_price_usd=2000.0)
    assert big / 100 < small          # bigger exit gets a worse average price
    assert exact is True              # a curve gives a real sell quote


def test_unpriceable_venue_marks_to_zero_and_counts_a_failure():
    # Correction #1: `if val is None: continue` preserved the last good mark
    # forever, so a honeypot showed a beautiful P&L until you tried to leave.
    pos = _pos(mark_usd=250.0)
    P.mark(pos, _v(pricing_reserves=None), quote_price_usd=2000.0)
    assert pos.mark_usd == 0.0
    assert pos.fails == 1


def test_a_good_mark_resets_the_failure_counter():
    pos = _pos(fails=2)
    P.mark(pos, _v(), quote_price_usd=2000.0)
    assert pos.fails == 0 and pos.mark_usd > 0


def test_exit_value_distinguishes_worthless_unknowable_and_approximate():
    # Three distinct outcomes. Collapsing any two of them is a bug — collapsing
    # "cannot quote this venue type" into "cannot sell" manufactured a fake -100%
    # and stopped out every Pons v1 position the moment it opened.
    assert P.exit_value_usd(_v(), 0, quote_price_usd=2000.0) == (0.0, True)
    # V3: no reserves, but an exact spot price -> approximate mark, not a rug
    v3 = _v(pricing_reserves=None, spot_price_quote_per_token=1e-8)
    val, exact = P.exit_value_usd(v3, MM, quote_price_usd=2000.0)
    assert val > 0 and exact is False
    # genuinely unpriceable -> None, which mark() turns into the rug path
    assert P.exit_value_usd(_v(pricing_reserves=None), MM,
                            quote_price_usd=2000.0) == (None, False)


def test_a_v3_position_is_not_stopped_out_the_moment_it_opens():
    # The live paper run caught this: every pons-v1 buy marked $0.00 and closed
    # at "stop -100%" on the very next mark.
    v3 = _v(kind=V.PONS_V1, pricing_reserves=None,
            spot_price_quote_per_token=1e-8)
    # 1e6 whole tokens at 1e-8 quote each, quote at $2000 => a ~$20 mark against
    # a $20 cost, i.e. a flat position that must simply be held.
    pos = _pos(cost_usd=20.0, tokens=10**24, peak_usd=20.0)
    P.mark(pos, v3, quote_price_usd=2000.0)
    assert abs(pos.mark_usd - 20.0) < 0.01
    assert pos.mark_usd > 0
    assert pos.mark_is_exact is False
    assert pos.fails == 0
    assert P.exit_decision(pos)[0] is None


def test_an_approximate_mark_is_flagged_so_it_is_never_mistaken_for_a_quote():
    exact = _pos()
    P.mark(exact, _v(), quote_price_usd=2000.0)
    assert exact.mark_is_exact is True


def test_peak_tracks_the_high_water_mark():
    pos = _pos(cost_usd=100.0, peak_usd=100.0)
    pos.mark_usd, pos.peak_usd = 100.0, 100.0
    P.mark(pos, _v(pricing_reserves=(40 * MM, 1000 * MM)), quote_price_usd=2000.0)
    high = pos.peak_usd
    P.mark(pos, _v(pricing_reserves=(1 * MM, 1000 * MM)), quote_price_usd=2000.0)
    assert pos.peak_usd == high        # peak never retreats


# --- exit policy -------------------------------------------------------------

def test_repeated_sell_failures_force_a_close_as_a_rug():
    pos = _pos(fails=P.RUG_FAILS, mark_usd=0.0)
    action, frac, why = P.exit_decision(pos)
    assert action == "close" and frac == 1.0 and "rug" in why


def test_rug_check_outranks_everything_including_a_winning_mark():
    # A honeypot can print a great mark; being unable to sell must still win.
    pos = _pos(fails=P.RUG_FAILS, mark_usd=1000.0)
    assert P.exit_decision(pos)[0] == "close"


def test_hard_stop_fires_below_the_floor():
    pos = _pos(mark_usd=50.0)          # -50%
    action, frac, why = P.exit_decision(pos)
    assert action == "close" and frac == 1.0 and "stop" in why


def test_trailing_stop_protects_a_run():
    # Armed at +25%, gives back 30% off peak — what stops a winner
    # round-tripping to flat. Marked BELOW the first rung so the trail is what
    # fires: a rung outranks it on the way up, by design.
    pos = _pos(mark_usd=130.0, peak_usd=200.0)
    action, _frac, why = P.exit_decision(pos)
    assert action == "close" and "trail" in why


def test_trailing_stop_stays_disarmed_below_the_arm_level():
    pos = _pos(mark_usd=105.0, peak_usd=120.0)   # peak only +20%
    assert P.exit_decision(pos)[0] is None


def test_rungs_trim_once_each_on_the_way_up():
    # First rung at +40%: the modal good outcome is a launch that runs 40-90%
    # and fades, and a ladder starting at 2x banked nothing on every one of them.
    pos = _pos(mark_usd=150.0, peak_usd=150.0)   # 1.5x
    action, frac, why = P.exit_decision(pos)
    assert action == "trim" and frac == 0.33 and "1.4x" in why
    # the same rung must not fire twice
    assert P.exit_decision(pos)[0] is None


def test_a_moon_bag_survives_every_rung():
    # The ladder must never add to 100%, or a runner gets trimmed to nothing
    # exactly when it is working.
    pos = _pos(mark_usd=600.0, peak_usd=600.0)
    trimmed = 0.0
    for _ in range(len(P.RUNGS)):
        action, frac, _why = P.exit_decision(pos)
        if action == "trim":
            trimmed += frac
    assert trimmed < 1.0
    assert sum(f for _, f in P.RUNGS) < 1.0


def test_stop_outranks_a_rung():
    # A position can be below the stop and above a rung only in weird books; the
    # stop must win regardless.
    pos = _pos(mark_usd=50.0)
    assert P.exit_decision(pos)[0] == "close"


def test_liquidity_collapse_closes_the_position():
    pos = _pos(mark_usd=110.0, peak_usd=110.0)
    P.exit_decision(pos, liquidity_usd=1000.0)          # establishes the baseline
    action, frac, why = P.exit_decision(pos, liquidity_usd=200.0)
    assert action == "close" and "liquidity" in why


def test_time_stop_only_applies_when_nothing_was_banked():
    old = time.time() - (P.MAX_HOLD_MIN + 5) * 60
    flat = _pos(mark_usd=110.0, peak_usd=110.0, opened_at=old)
    assert P.exit_decision(flat)[0] == "close"
    banked = _pos(mark_usd=110.0, peak_usd=110.0, opened_at=old, rungs_hit=[0])
    assert P.exit_decision(banked)[0] is None


def test_an_unmarked_position_does_not_trade():
    assert P.exit_decision(_pos(mark_usd=None))[0] is None


# --- applying fills ----------------------------------------------------------

def test_close_zeroes_the_position():
    after = P.apply(_pos(), "close", 1.0)
    assert after.tokens == 0 and after.closed is True


def test_trim_reduces_tokens_and_cost_basis_together():
    after = P.apply(_pos(tokens=10 * MM, cost_usd=100.0), "trim", 0.5)
    assert after.tokens == 5 * MM and after.cost_usd == 50.0
    assert after.closed is False


def test_no_action_leaves_the_position_untouched():
    p = _pos()
    assert P.apply(p, None, 0.0) is p


# --- book --------------------------------------------------------------------

def test_portfolio_summary_reports_pnl_and_unmarked_exposure():
    book = [_pos(cost_usd=100.0, mark_usd=150.0),
            _pos(cost_usd=100.0, mark_usd=None),
            _pos(cost_usd=50.0, mark_usd=0.0, fails=1),
            _pos(closed=True, cost_usd=999.0, mark_usd=999.0)]
    s = P.portfolio_summary(book)
    assert s["open"] == 3
    assert s["cost_usd"] == 250.0 and s["value_usd"] == 150.0
    assert s["pnl_usd"] == -100.0
    assert s["unmarked"] == 1 and s["at_risk"] == 1


# --- the rug check must reach spot-marked venues too -------------------------

def test_spot_marked_venues_can_still_be_caught_as_unsellable():
    # The hole the spot fallback opened: spot ALWAYS returns a number, so `fails`
    # never incremented and a V3/v4 honeypot marked healthy forever. Spot reflects
    # the pool's price, not whether you personally can sell.
    v3 = _v(kind=V.PONS_V1, pricing_reserves=None, spot_price_quote_per_token=1e-8)
    pos = _pos(cost_usd=20.0, tokens=10**24, peak_usd=20.0)
    quoter = lambda venue, tokens: (None, "unsellable")
    for _ in range(P.RUG_FAILS):
        P.mark(pos, v3, quote_price_usd=2000.0, exit_quoter=quoter)
    assert pos.fails == P.RUG_FAILS and pos.mark_usd == 0.0
    action, frac, why = P.exit_decision(pos)
    assert action == "close" and "rug" in why


def test_a_network_error_is_not_treated_as_a_rug():
    # Closing good positions on a bad connection is its own kind of loss.
    v3 = _v(kind=V.PONS_V1, pricing_reserves=None, spot_price_quote_per_token=1e-8)
    pos = _pos(cost_usd=20.0, tokens=10**24, peak_usd=20.0)
    quoter = lambda venue, tokens: (None, "error")
    for _ in range(P.RUG_FAILS + 2):
        P.mark(pos, v3, quote_price_usd=2000.0, exit_quoter=quoter)
    assert pos.fails == 0
    assert pos.mark_usd > 0
    assert P.exit_decision(pos)[0] is None


def test_a_real_flash_quote_upgrades_the_mark_to_exact():
    v3 = _v(kind=V.PONS_V1, pricing_reserves=None, spot_price_quote_per_token=1e-8)
    pos = _pos(cost_usd=20.0, tokens=10**24, peak_usd=20.0)
    P.mark(pos, v3, quote_price_usd=2000.0,
           exit_quoter=lambda venue, tokens: (17.5, "ok"))
    assert pos.mark_usd == 17.5          # the real exit, below the $20 spot
    assert pos.mark_is_exact is True


def test_curve_venues_never_pay_for_a_quote_they_do_not_need():
    # An exact local quote must not be overridden by a network call.
    calls = []
    P.mark(_pos(), _v(), quote_price_usd=2000.0,
           exit_quoter=lambda venue, tokens: calls.append(1) or (99.0, "ok"))
    assert calls == []


# --- stall: dead volume is a sell, not a wait --------------------------------

def test_a_position_making_no_new_highs_is_closed():
    # The desk is meant to trade at a fairly consistent rate, not buy and hold.
    # No new high means no buyers, and on a fresh launch that is the thesis gone.
    import time as _t
    pos = _pos(mark_usd=98.0, peak_usd=105.0)
    pos.peak_at = _t.time() - (P.STALL_S + 5)
    action, frac, why = P.exit_decision(pos)
    assert action == "close" and frac == 1.0
    assert "stalled" in why


def test_a_fresh_position_is_given_its_time():
    import time as _t
    pos = _pos(mark_usd=98.0, peak_usd=105.0)
    pos.peak_at = _t.time() - 5
    assert P.exit_decision(pos)[0] is None


def test_a_position_that_has_banked_is_left_to_consolidate():
    # A winner going sideways after a rung is not a stall — it is a moon bag,
    # and cutting it there is exactly the mistake the ladder exists to avoid.
    import time as _t
    pos = _pos(mark_usd=180.0, peak_usd=200.0)
    pos.rungs_hit = [0]
    pos.peak_at = _t.time() - (P.STALL_S + 60)
    assert P.exit_decision(pos)[0] is None


def test_the_peak_clock_only_resets_on_a_NEW_high():
    # Re-stamping it on every mark would mean the stall never fires.
    import time as _t
    pos = _pos(mark_usd=100.0, peak_usd=100.0)
    P.mark_from_exit_quote(pos, 120.0)
    first = pos.peak_at
    assert first > 0
    _t.sleep(0.01)
    P.mark_from_exit_quote(pos, 110.0)       # lower — not a new high
    assert pos.peak_at == first
    P.mark_from_exit_quote(pos, 130.0)       # new high
    assert pos.peak_at > first


def test_a_stop_still_outranks_a_stall():
    import time as _t
    pos = _pos(mark_usd=50.0, peak_usd=105.0)
    pos.peak_at = _t.time() - (P.STALL_S + 5)
    assert "stop" in P.exit_decision(pos)[2]
