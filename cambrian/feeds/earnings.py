"""Earnings calendar.

The LP desk stays out of a name across its earnings report. That requires
knowing the next report date per ticker. There is no free, reliable, key-less
earnings API, so the default source is one you maintain by hand — a JSON file
mapping ticker to the next report date. Anything not in the file returns
`None`, and the LP desk treats `None` as "inside the blackout": if we can't
prove we're clear of earnings, we don't provide.

Plug a real provider (an equities data vendor, your broker's calendar) in behind
the same `days_to_earnings` interface when you have one.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Protocol


class EarningsCalendar(Protocol):
    def days_to_earnings(self, ticker: str, now_utc: datetime) -> float | None: ...


class ManualEarningsCalendar:
    """Backed by a dict of {TICKER: 'YYYY-MM-DD'} (next report date). Load from a
    JSON file with `from_file`, or construct directly in tests."""

    def __init__(self, dates: dict[str, str] | None = None):
        self._dates: dict[str, date] = {}
        for ticker, iso in (dates or {}).items():
            self._dates[ticker.upper()] = date.fromisoformat(iso)

    @classmethod
    def from_file(cls, path: str) -> "ManualEarningsCalendar":
        if not os.path.exists(path):
            return cls({})
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def days_to_earnings(self, ticker: str, now_utc: datetime) -> float | None:
        d = self._dates.get(ticker.upper())
        if d is None:
            return None
        return float((d - now_utc.date()).days)
