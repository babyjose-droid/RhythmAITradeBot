from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import (
    Exchange,
    InstrumentKey,
    is_tick_multiple,
    round_to_tick,
    to_decimal,
)

CAL = SessionCalendar()


def ist(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=EXCHANGE_TZ)


# ------------------------------------------------------------------ market vocabulary
def test_instrument_key_roundtrip() -> None:
    k = InstrumentKey.parse("nse:mocka")
    assert k == InstrumentKey(Exchange.NSE, "MOCKA")
    assert str(k) == "NSE:MOCKA"


@pytest.mark.parametrize("bad", ["", "lower", "A B", "X" * 33, "-ABC"])
def test_invalid_symbols_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        InstrumentKey(Exchange.NSE, bad)


def test_parse_requires_exchange() -> None:
    with pytest.raises(ValueError, match="EXCHANGE:SYMBOL"):
        InstrumentKey.parse("MOCKA")


def test_decimal_conversion_avoids_float_artefacts() -> None:
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal("92.50") == Decimal("92.50")
    with pytest.raises(TypeError):
        to_decimal(True)


def test_tick_rounding() -> None:
    assert round_to_tick(Decimal("100.03"), Decimal("0.05")) == Decimal("100.05")
    assert round_to_tick(Decimal("100.02"), Decimal("0.05")) == Decimal("100.00")
    assert is_tick_multiple(Decimal("100.05"), Decimal("0.05"))
    assert not is_tick_multiple(Decimal("100.03"), Decimal("0.05"))
    with pytest.raises(ValueError):
        round_to_tick(Decimal(1), Decimal(0))


# ------------------------------------------------------------------ calendar
def test_weekends_are_not_trading_days() -> None:
    assert CAL.is_trading_day(date(2026, 10, 30))  # Friday
    assert not CAL.is_trading_day(date(2026, 10, 31))  # Saturday
    assert not CAL.is_trading_day(date(2026, 11, 1))  # Sunday


def test_session_boundaries() -> None:
    assert not CAL.is_session_open(ist(2026, 10, 30, 9, 14, 59))
    assert CAL.is_session_open(ist(2026, 10, 30, 9, 15))
    assert CAL.is_session_open(ist(2026, 10, 30, 15, 29, 59))
    assert not CAL.is_session_open(ist(2026, 10, 30, 15, 30))
    assert not CAL.is_session_open(ist(2026, 10, 31, 11, 0))


def test_session_check_accepts_utc() -> None:
    assert CAL.is_session_open(datetime(2026, 10, 30, 4, 0, tzinfo=UTC))  # 09:30 IST


def test_last_completed_session() -> None:
    assert CAL.last_completed_session(ist(2026, 10, 30, 15, 30)) == date(2026, 10, 30)
    assert CAL.last_completed_session(ist(2026, 10, 30, 12, 0)) == date(2026, 10, 29)
    assert CAL.last_completed_session(ist(2026, 11, 2, 9, 0)) == date(2026, 10, 30)  # Monday am


def test_holidays_are_respected_when_supplied() -> None:
    cal = SessionCalendar(holidays=[date(2026, 10, 29)], holidays_verified=True)
    assert not cal.is_trading_day(date(2026, 10, 29))
    assert cal.previous_trading_day(date(2026, 10, 30)) == date(2026, 10, 28)


def test_holidays_default_unverified() -> None:
    assert SessionCalendar().holidays_verified is False
