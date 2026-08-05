from datetime import datetime, timezone

from cambrian.feeds import market_hours as mh


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_midday_trading_day_is_open():
    # 2026-08-05 is a Wednesday. 15:00 UTC == 11:00 ET (EDT).
    s = mh.session(utc(2026, 8, 5, 15, 0))
    assert s.market_open
    assert round(s.minutes_since_open) == 90     # 09:30 -> 11:00
    assert round(s.minutes_to_close) == 300      # 11:00 -> 16:00


def test_before_open_is_closed():
    s = mh.session(utc(2026, 8, 5, 13, 0))        # 09:00 ET
    assert not s.market_open


def test_weekend_is_closed():
    s = mh.session(utc(2026, 8, 8, 15, 0))        # Saturday
    assert not s.market_open


def test_holiday_is_closed():
    s = mh.session(utc(2026, 12, 25, 15, 0))      # Christmas
    assert not s.market_open


def test_uncovered_year_fails_closed_and_flags_stale():
    s = mh.session(utc(2030, 8, 5, 15, 0))
    assert not s.market_open
    assert s.calendar_stale


def test_naive_datetime_rejected():
    import pytest
    with pytest.raises(ValueError):
        mh.session(datetime(2026, 8, 5, 15, 0))
