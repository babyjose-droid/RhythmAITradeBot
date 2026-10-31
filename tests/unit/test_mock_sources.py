from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rhythm.broker.base import (
    BrokerAuthError,
    BrokerRateLimitedError,
    BrokerReadAdapter,
    BrokerUnavailableError,
)
from rhythm.broker.mock import MOCK_SYMBOLS, MockBrokerAdapter, MockBrokerScenario
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey
from rhythm.marketdata.freshness import FreshnessStatus, evaluate_quote
from rhythm.marketdata.mock import MockMarketDataProvider, MockScenario
from rhythm.marketdata.provider import MarketDataProvider, MarketDataUnavailableError
from rhythm.marketdata.types import Interval
from rhythm.marketdata.validation import bar_issues

CAL = SessionCalendar()
IN_SESSION = datetime(2026, 10, 30, 11, 0, tzinfo=EXCHANGE_TZ)
A = InstrumentKey(Exchange.NSE, "MOCKA")
B = InstrumentKey(Exchange.NSE, "MOCKB")


def provider(at: datetime = IN_SESSION, **scenario: object) -> MockMarketDataProvider:
    return MockMarketDataProvider(
        clock=FixedClock(at),
        calendar=CAL,
        scenario=MockScenario(**scenario),  # type: ignore[arg-type]
    )


def test_protocols_satisfied() -> None:
    assert isinstance(provider(), MarketDataProvider)
    assert isinstance(MockBrokerAdapter(), BrokerReadAdapter)


def test_broker_interface_has_no_order_methods() -> None:
    names = set(dir(BrokerReadAdapter)) | set(dir(MockBrokerAdapter))
    for forbidden in ("place_order", "modify_order", "cancel_order", "placeOrder", "submit"):
        assert forbidden not in names


def test_quotes_are_deterministic_labelled_and_fresh_in_session() -> None:
    q1, q2 = provider().latest_quote(A), provider().latest_quote(A)
    assert q1 == q2 and q1 is not None
    assert q1.source is DataOrigin.MOCK and q1.is_mock
    assert evaluate_quote(q1, IN_SESSION, CAL).status is FreshnessStatus.FRESH
    assert provider().latest_quote(B) != q1


def test_quote_outside_session_is_last_close() -> None:
    sat = datetime(2026, 10, 31, 12, 0, tzinfo=EXCHANGE_TZ)
    q = provider(sat).latest_quote(A)
    assert q is not None
    assert q.source_ts.astimezone(EXCHANGE_TZ) == datetime(
        2026, 10, 30, 15, 29, 59, tzinfo=EXCHANGE_TZ
    )
    assert evaluate_quote(q, sat, CAL).status is FreshnessStatus.SESSION_CLOSED


def test_failure_injection() -> None:
    assert provider(missing={A}).latest_quote(A) is None
    with pytest.raises(MarketDataUnavailableError):
        provider(errors={A}).latest_quote(A)
    with pytest.raises(MarketDataUnavailableError):
        provider(outage=True).bars(A, Interval.D1, IN_SESSION - timedelta(days=5), IN_SESSION)
    stale = provider(stale={A: timedelta(minutes=2)}).latest_quote(A)
    assert evaluate_quote(stale, IN_SESSION, CAL).status is FreshnessStatus.STALE
    fut = provider(future={A}).latest_quote(A)
    assert evaluate_quote(fut, IN_SESSION, CAL).status is FreshnessStatus.INVALID
    crossed = provider(crossed={A}).latest_quote(A)
    assert evaluate_quote(crossed, IN_SESSION, CAL).status is FreshnessStatus.INVALID


def test_daily_bars_only_completed_trading_sessions() -> None:
    # Window Sat 24 Oct .. Fri 30 Oct 11:00: Mon-Thu completed, Fri still open.
    bars = provider().bars(A, Interval.D1, IN_SESSION - timedelta(days=6, hours=2), IN_SESSION)
    days = [b.start.astimezone(EXCHANGE_TZ).date().isoformat() for b in bars]
    assert days == ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    assert all(b.is_mock for b in bars)


def test_intraday_bars_valid_and_aligned() -> None:
    sat = datetime(2026, 10, 31, 12, 0, tzinfo=EXCHANGE_TZ)
    day_start = datetime(2026, 10, 30, 0, 0, tzinfo=EXCHANGE_TZ)
    bars = provider(sat).bars(A, Interval.M5, day_start, sat)
    assert len(bars) == 75  # 09:15-15:30 = 375 minutes / 5
    assert bars[0].start.astimezone(EXCHANGE_TZ).strftime("%H:%M") == "09:15"
    for b in bars:
        assert bar_issues(b, sat) == []


def test_unsupported_interval() -> None:
    with pytest.raises(MarketDataUnavailableError):
        provider().bars(A, Interval.W1, IN_SESSION - timedelta(days=30), IN_SESSION)


def test_mock_broker_fixtures_and_failures() -> None:
    b = MockBrokerAdapter(clock=FixedClock(IN_SESSION))
    master = b.get_scrip_master(Exchange.NSE)
    assert [r.symbol for r in master] == list(MOCK_SYMBOLS)
    assert all(r.source is DataOrigin.MOCK and r.isin is None for r in master)
    assert b.get_profile().account_ref.startswith("*")
    assert b.get_holdings()[0].origin is DataOrigin.MOCK
    for scenario, err in [
        (MockBrokerScenario(auth_expired=True), BrokerAuthError),
        (MockBrokerScenario(rate_limited=True), BrokerRateLimitedError),
        (MockBrokerScenario(unavailable=True), BrokerUnavailableError),
    ]:
        with pytest.raises(err):
            MockBrokerAdapter(scenario=scenario).get_funds(Exchange.NSE)
