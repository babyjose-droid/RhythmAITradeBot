"""Exchange session calendar (NSE/BSE cash equity, regular session only).

Regular session 09:15-15:30 IST, Monday-Friday. Exchange holidays are NOT bundled:
they must be loaded from the exchange's published list (see KNOWN_LIMITATIONS.md). Until
holidays are supplied, a holiday is treated as a trading day, which makes freshness checks
report STALE on holidays — the safe (fail-closed) direction.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from rhythm.core.clock import EXCHANGE_TZ, ensure_utc

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


class SessionCalendar:
    def __init__(self, holidays: Iterable[date] = (), *, holidays_verified: bool = False) -> None:
        self._holidays = frozenset(holidays)
        self.holidays_verified = holidays_verified

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self._holidays

    def session_open(self, d: date) -> datetime:
        return datetime.combine(d, SESSION_OPEN, tzinfo=EXCHANGE_TZ)

    def session_close(self, d: date) -> datetime:
        return datetime.combine(d, SESSION_CLOSE, tzinfo=EXCHANGE_TZ)

    def is_session_open(self, at: datetime) -> bool:
        local = ensure_utc(at).astimezone(EXCHANGE_TZ)
        d = local.date()
        return self.is_trading_day(d) and self.session_open(d) <= local < self.session_close(d)

    def previous_trading_day(self, d: date) -> date:
        cur = d - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise ValueError("no trading day found in the previous 30 days")

    def last_completed_session(self, at: datetime) -> date:
        """Date of the most recent session that has closed at or before ``at``."""
        local = ensure_utc(at).astimezone(EXCHANGE_TZ)
        d = local.date()
        if self.is_trading_day(d) and local >= self.session_close(d):
            return d
        return self.previous_trading_day(d)

    def current_or_last_session_open(self, at: datetime) -> datetime:
        """Open time of the session in progress, or of the last session if none is open."""
        local = ensure_utc(at).astimezone(EXCHANGE_TZ)
        d = local.date()
        if self.is_trading_day(d) and local >= self.session_open(d):
            return self.session_open(d)
        return self.session_open(self.previous_trading_day(d))
