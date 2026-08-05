"""Assemble an `LPContext` from the individual feeds.

Kept out of the desk so the desk stays pure. This is the glue that resolves
"now" + a ticker into the market-hours and earnings facts the LP desk compares
against.
"""

from __future__ import annotations

from datetime import datetime

from ..snapshots import LPContext
from . import earnings as earnings_mod
from . import market_hours as mh


def build_lp_context(
    ticker: str,
    now_utc: datetime,
    earnings: earnings_mod.EarningsCalendar,
    calendar: mh.MarketCalendar = mh.DEFAULT_CALENDAR,
) -> LPContext:
    sess = mh.session(now_utc, calendar)
    return LPContext(
        now_utc=now_utc,
        ticker=ticker,
        market_open=sess.market_open,
        minutes_since_open=sess.minutes_since_open,
        minutes_to_close=sess.minutes_to_close,
        days_to_earnings=earnings.days_to_earnings(ticker, now_utc),
    )
