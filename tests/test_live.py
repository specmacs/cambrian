"""The live loop: two cadences, dedup, and intents that never mutate the book."""

import time

from cambrian.runners import live as L
from cambrian.runners import positions as P
from cambrian.runners import venues as V

MM = 10 ** 18


def _venue(**kw) -> V.Venue:
    base = dict(kind=V.PONS_V2, token="0xtok", pool="0xcurve",
                quote_token=V.NATIVE_ETH, quote_decimals=18, token_decimals=18,
                pricing_reserves=(10 * MM, 1000 * MM), fee_bps=100, tax_bps=0,
                total_supply=10**27, graduated=False, sellable_tokens=10**30)
    base.update(kw)
    return V.Venue(**base)


# --- cadence -----------------------------------------------------------------

def test_marking_is_due_far_more_often_than_scanning():
    # A stop that fires 30s late on a memecoin is a stop that did not fire, so
    # marking must never wait on a slow sweep.
    assert L.MARK_INTERVAL_S < L.SCAN_INTERVAL_S
    st = L.LiveState(last_scan_at=1000.0, last_mark_at=1000.0)
    scan_due, mark_due = L.due(st, now=1000.0 + L.MARK_INTERVAL_S + 0.1)
    assert mark_due is True and scan_due is False


def test_both_come_due_once_the_scan_interval_passes():
    st = L.LiveState(last_scan_at=1000.0, last_mark_at=1000.0)
    assert L.due(st, now=1000.0 + L.SCAN_INTERVAL_S + 1) == (True, True)


# --- dedup -------------------------------------------------------------------

def test_a_token_alerts_once():
    st = L.LiveState()
    assert st.note_seen("0xAbC") is True
    assert st.note_seen("0xabc") is False       # case-insensitive


def test_the_seen_set_is_bounded_so_a_long_run_cannot_leak():
    st = L.LiveState()
    for i in range(L.SEEN_MAX + 25):
        st.note_seen("0x%040x" % i)
    assert len(st.seen) == L.SEEN_MAX
    assert len(st._seen_set) == L.SEEN_MAX
    assert st.note_seen("0x%040x" % 0) is True   # oldest was evicted


# --- marking and intents -----------------------------------------------------

def _state_with_position(**venue_kw):
    st = L.LiveState()
    v = _venue(**venue_kw)
    L.record_fill(st, token="0xtok", venue=v, tokens=10 * MM, cost_usd=100.0)
    return st, v


def test_mark_tick_values_positions_and_emits_no_intent_when_holding():
    st, _ = _state_with_position()
    # Put the mark in the hold band: the fixture's venue values this position
    # well above the first rung, and a rung firing is correct behaviour, not the
    # thing under test here.
    st.positions["0xtok"].cost_usd = 196.04 / 1.05   # ~+5%, inside the hold band
    intents = L.mark_tick(None, st, weth_usd=2000.0)
    assert intents == []
    assert st.positions["0xtok"].mark_usd is not None


def test_mark_tick_emits_an_exit_intent_when_the_policy_fires():
    st, _ = _state_with_position()
    st.positions["0xtok"].cost_usd = 100_000.0      # mark now far below the stop
    intents = L.mark_tick(None, st, weth_usd=2000.0)
    assert intents and intents[0]["action"] == "close"
    assert "stop" in intents[0]["why"]


def test_an_intent_does_not_mutate_the_position():
    # The book changes only on a fill. Updating on intent makes it disagree with
    # the chain, which is worse than not tracking at all.
    st, _ = _state_with_position()
    st.positions["0xtok"].cost_usd = 100_000.0
    before = st.positions["0xtok"].tokens
    L.mark_tick(None, st, weth_usd=2000.0)
    assert st.positions["0xtok"].tokens == before
    assert st.positions["0xtok"].closed is False


def test_an_unpriceable_position_marks_to_zero_rather_than_freezing():
    # Correction #1 again, at loop level: skipping a failed valuation would leave
    # the previous mark standing, which is how a honeypot reads as a winner.
    st, _ = _state_with_position(pricing_reserves=None)
    st.positions["0xtok"].mark_usd = 500.0
    L.mark_tick(None, st, weth_usd=2000.0)
    assert st.positions["0xtok"].mark_usd == 0.0
    assert st.positions["0xtok"].fails == 1


def test_closed_positions_are_not_re_marked():
    st, _ = _state_with_position()
    L.record_exit_fill(st, token="0xtok", action="close", fraction=1.0)
    assert L.mark_tick(None, st, weth_usd=2000.0) == []


# --- fills -------------------------------------------------------------------

def test_a_fill_opens_the_position_and_remembers_its_venue():
    st, v = _state_with_position()
    assert st.positions["0xtok"].cost_usd == 100.0
    assert st.venues["0xtok"] is v


def test_an_exit_fill_reduces_the_book():
    st, _ = _state_with_position()
    after = L.record_exit_fill(st, token="0xtok", action="trim", fraction=0.5)
    assert after.tokens == 5 * MM and after.closed is False
    closed = L.record_exit_fill(st, token="0xtok", action="close", fraction=1.0)
    assert closed.closed is True


def test_an_exit_fill_for_an_unknown_token_is_a_no_op():
    assert L.record_exit_fill(L.LiveState(), token="0xnope",
                              action="close", fraction=1.0) is None


# --- output ------------------------------------------------------------------

def test_a_quiet_tick_prints_nothing():
    # A loop that logs every tick buries the ticks that matter.
    assert L.format_tick([], [], L.LiveState()) == ""


def test_a_tick_with_activity_reports_new_rows_exits_and_the_book():
    st, _ = _state_with_position()
    L.mark_tick(None, st, weth_usd=2000.0)
    fresh = [{"pad": "pons-v2", "token": "0xnew", "quote_symbol": "GME",
              "market_cap_usd": 3104.0, "tax_bps": 400, "size_usd": 15.52}]
    intents = [{"token": "0xtok", "action": "close", "fraction": 1.0,
                "why": "stop -50%", "mark_usd": 50.0, "cost_usd": 100.0,
                "venue": V.PONS_V2}]
    out = L.format_tick(fresh, intents, st)
    assert "NEW" in out and "GME" in out
    assert "EXIT" in out and "stop -50%" in out and "-50.0%" in out
    assert "book:" in out
