from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from rhythm.config import Environment
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.db.models import DataQualityEvent
from rhythm.db.session import session_scope
from rhythm.instruments.registry import MockDataNotAllowedError
from rhythm.marketdata.bars import get_bars, latest_bar_start, upsert_bars
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.types import Interval

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 31, 12, 0, tzinfo=EXCHANGE_TZ)
CLOCK = FixedClock(AT)
A = InstrumentKey(Exchange.NSE, "MOCKA")
PROVIDER = MockMarketDataProvider(clock=CLOCK, calendar=SessionCalendar())


def test_invalid_bars_rejected_with_quality_event(session_factory: sessionmaker[Session]) -> None:
    bars = list(PROVIDER.bars(A, Interval.D1, AT - timedelta(days=7), AT))
    bars[0] = replace(bars[0], high=bars[0].low - Decimal("1"))
    with session_scope(session_factory) as s:
        res = upsert_bars(s, bars, clock=CLOCK, environment=Environment.TEST)
    assert res.rejected == 1 and res.written == len(bars) - 1
    with session_scope(session_factory) as s:
        assert s.execute(select(DataQualityEvent.kind)).scalars().all() == ["INVALID_BAR"]
        stored = get_bars(s, A, Interval.D1, AT - timedelta(days=7), AT)
        assert len(stored) == len(bars) - 1
        assert latest_bar_start(s, A, Interval.D1) == bars[-1].start


def test_mock_bars_refused_in_real_environments(session_factory: sessionmaker[Session]) -> None:
    bars = PROVIDER.bars(A, Interval.D1, AT - timedelta(days=3), AT)
    for env in (Environment.STAGING, Environment.PRODUCTION):
        with pytest.raises(MockDataNotAllowedError), session_scope(session_factory) as s:
            upsert_bars(s, bars, clock=CLOCK, environment=env)


def test_intraday_bars_roundtrip_precision(session_factory: sessionmaker[Session]) -> None:
    day = datetime(2026, 10, 30, 0, 0, tzinfo=EXCHANGE_TZ)
    bars = PROVIDER.bars(A, Interval.M5, day, AT)
    with session_scope(session_factory) as s:
        upsert_bars(s, bars, clock=CLOCK, environment=Environment.TEST)
    with session_scope(session_factory) as s:
        stored = get_bars(s, A, Interval.M5, day, AT)
    assert len(stored) == 75
    assert [r.close for r in stored] == [b.close for b in bars]  # Decimal exact
