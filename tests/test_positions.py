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
    clean_val = P.exit_value_usd(v, 10 * MM, quote_price_usd=2000.0)
    taxed_val = P.exit_value_usd(taxed, 10 * MM, quote_price_usd=2000.0)
    assert taxed_val < clean_val


def test_mark_includes_slippage_of_the_actual_size():
    v = _v()
    small = P.exit_value_usd(v, MM, quote_price_usd=2000.0)
    big = P.exit_value_usd(v, 100 * MM, quote_price_usd=2000.0)
    assert big / 100 < small          # bigger exit gets a worse average price


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


def test_exit_value_distinguishes_worthless_from_unknowable():
    # 0 and None are different facts with different responses.
    assert P.exit_value_usd(_v(), 0, quote_price_usd=2000.0) == 0.0
    assert P.exit_value_usd(_v(pricing_reserves=None), MM,
                            quote_price_usd=2000.0) is None


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
    # Armed at +35%, gives back 22% off peak. This is what stops a +80% winner
    # round-tripping to flat.
    pos = _pos(mark_usd=150.0, peak_usd=200.0)
    action, frac, why = P.exit_decision(pos)
    assert action == "close" and "trail" in why


def test_trailing_stop_stays_disarmed_below_the_arm_level():
    pos = _pos(mark_usd=105.0, peak_usd=120.0)   # peak only +20%
    assert P.exit_decision(pos)[0] is None


def test_rungs_trim_once_each_on_the_way_up():
    pos = _pos(mark_usd=250.0, peak_usd=250.0)   # 2.5x
    action, frac, why = P.exit_decision(pos)
    assert action == "trim" and frac == 0.50 and "2x" in why
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
