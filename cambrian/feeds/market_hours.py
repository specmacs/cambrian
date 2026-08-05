"""US equity regular-trading-hours calendar.

The LP desk provides liquidity against tokenized equities, so it needs to know
where "now" sits relative to the underlying's cash session: is it open, how long
since the 09:30 ET open, how long until the 16:00 ET close.

Holidays are the trap. A naive weekday check reports the market open on
Thanksgiving, and the LP desk would happily deploy into a session that never
starts. So this calendar fails closed twice over:

  * Known holidays are excluded.
  * If asked about a date in a year the holiday list does not cover, it reports
    the market CLOSED and flags the calendar stale — rather than guessing.

The bundled holiday dates are a convenience for 2025-2026 and MUST be verified
against the official NYSE calendar, including early-close half-days (which are
not modeled here — extend `MarketCalendar` if intraday half-days matter to you).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception as exc:  # pragma: no cover - platform without tzdata
    raise RuntimeError(
        "America/New_York timezone unavailable; install tzdata "
        "(`pip install tzdata`) so market hours are correct"
    ) from exc

OPEN = time(9, 30)
CLOSE = time(16, 0)

# NYSE full-day closures. VERIFY against the official calendar before trusting.
_HOLIDAYS_2025 = {
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
}
_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
}


@dataclass(frozen=True)
class MarketCalendar:
    holidays: frozenset[date]
    covered_years: frozenset[int]

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def covers(self, d: date) -> bool:
        return d.year in self.covered_years


DEFAULT_CALENDAR = MarketCalendar(
    holidays=frozenset(_HOLIDAYS_2025 | _HOLIDAYS_2026),
    covered_years=frozenset({2025, 2026}),
)


@dataclass(frozen=True)
class MarketSession:
    local_time: datetime
    market_open: bool
    minutes_since_open: float | None
    minutes_to_close: float | None
    #: Set when the calendar can't speak to this date; market_open is False then.
    calendar_stale: bool = False


def session(now_utc: datetime, calendar: MarketCalendar = DEFAULT_CALENDAR) -> MarketSession:
    """Where `now_utc` sits in the US cash session. `now_utc` must be timezone-
    aware; pass `datetime.now(timezone.utc)`."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    local = now_utc.astimezone(_NY)
    day = local.date()

    if not calendar.covers(day):
        # Beyond what we can vouch for. Fail closed.
        return MarketSession(local, False, None, None, calendar_stale=True)

    if not calendar.is_trading_day(day):
        return MarketSession(local, False, None, None)

    open_dt = datetime.combine(day, OPEN, tzinfo=_NY)
    close_dt = datetime.combine(day, CLOSE, tzinfo=_NY)
    is_open = open_dt <= local <= close_dt
    if not is_open:
        return MarketSession(local, False, None, None)

    since = (local - open_dt).total_seconds() / 60.0
    until = (close_dt - local).total_seconds() / 60.0
    return MarketSession(local, True, since, until)
