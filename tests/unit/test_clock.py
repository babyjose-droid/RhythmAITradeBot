from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from rhythm.core.clock import FixedClock, SystemClock, ensure_utc, to_exchange_local


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 1, 1))  # noqa: DTZ001 — deliberately naive


def test_exchange_local_is_ist() -> None:
    local = to_exchange_local(datetime(2026, 10, 30, 3, 45, tzinfo=UTC))
    assert local.utcoffset() == timedelta(hours=5, minutes=30)
    assert (local.hour, local.minute) == (9, 15)  # NSE open in IST


def test_ensure_utc_converts_offsets() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    assert ensure_utc(datetime(2026, 1, 1, 9, 15, tzinfo=ist)) == datetime(
        2026, 1, 1, 3, 45, tzinfo=UTC
    )


def test_fixed_clock() -> None:
    c = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    c.advance(timedelta(seconds=5))
    assert c.now() == datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)


def test_system_clock_is_aware_utc() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)
