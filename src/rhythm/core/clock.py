"""Time handling.

All persisted timestamps are UTC. Exchange-local context (IST) is derived, never stored
as the only representation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

EXCHANGE_TZ_NAME = "Asia/Kolkata"
EXCHANGE_TZ = ZoneInfo(EXCHANGE_TZ_NAME)


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic clock for tests."""

    def __init__(self, at: datetime) -> None:
        self._at = ensure_utc(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = ensure_utc(at)

    def advance(self, delta: timedelta) -> None:
        self._at = self._at + delta


def ensure_utc(dt: datetime) -> datetime:
    """Convert an aware datetime to UTC. Naive datetimes are rejected (ambiguous)."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime is not allowed; supply a timezone-aware value")
    return dt.astimezone(UTC)


def to_exchange_local(dt: datetime) -> datetime:
    """Convert an aware datetime to exchange-local time (IST)."""
    return ensure_utc(dt).astimezone(EXCHANGE_TZ)
