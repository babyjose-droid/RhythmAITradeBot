from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey
from rhythm.marketdata.freshness import (
    FreshnessPolicy,
    FreshnessStatus,
    evaluate_daily,
    evaluate_quote,
)
from rhythm.marketdata.types import Bar, Interval, Quote
from rhythm.marketdata.validation import bar_issues, quote_issues

CAL = SessionCalendar()
KEY = InstrumentKey(Exchange.NSE, "MOCKA")
IN_SESSION = datetime(2026, 10, 30, 11, 0, tzinfo=EXCHANGE_TZ)  # Friday 11:00 IST
AFTER_CLOSE = datetime(2026, 10, 30, 18, 0, tzinfo=EXCHANGE_TZ)
SATURDAY = datetime(2026, 10, 31, 10, 0, tzinfo=EXCHANGE_TZ)


def q(ts: datetime, **kw: object) -> Quote:
    base: dict[str, object] = {
        "key": KEY,
        "ltp": Decimal("100.00"),
        "bid": Decimal("99.95"),
        "ask": Decimal("100.05"),
        "source": DataOrigin.MOCK,
        "source_ts": ts,
        "received_ts": ts,
    }
    base.update(kw)
    return Quote(**base)  # type: ignore[arg-type]


def bar(**kw: object) -> Bar:
    base: dict[str, object] = {
        "key": KEY,
        "interval": Interval.D1,
        "start": datetime(2026, 10, 29, 9, 15, tzinfo=EXCHANGE_TZ),
        "open": Decimal(100),
        "high": Decimal(105),
        "low": Decimal(99),
        "close": Decimal(104),
        "volume": 10,
        "source": DataOrigin.MOCK,
        "received_ts": IN_SESSION,
    }
    base.update(kw)
    return Bar(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------ validation
def test_valid_bar_has_no_issues() -> None:
    assert bar_issues(bar(), IN_SESSION) == []


def test_invalid_bars_detected() -> None:
    assert bar_issues(bar(high=Decimal(101)), IN_SESSION)  # high < close
    assert bar_issues(bar(low=Decimal(0)), IN_SESSION)
    assert bar_issues(bar(volume=-1), IN_SESSION)
    assert bar_issues(bar(start=IN_SESSION + timedelta(days=1)), IN_SESSION)


def test_quote_validation() -> None:
    assert quote_issues(q(IN_SESSION), IN_SESSION) == []
    assert "crossed market (bid above ask)" in quote_issues(
        q(IN_SESSION, bid=Decimal(101), ask=Decimal(100)), IN_SESSION
    )
    assert quote_issues(q(IN_SESSION, ltp=Decimal(0)), IN_SESSION)
    assert quote_issues(
        q(IN_SESSION, lower_circuit=Decimal(90), upper_circuit=Decimal(95)), IN_SESSION
    )


# ------------------------------------------------------------------ quote freshness
def test_fresh_quote_in_session() -> None:
    r = evaluate_quote(q(IN_SESSION - timedelta(seconds=3)), IN_SESSION, CAL)
    assert r.status is FreshnessStatus.FRESH
    assert r.status.usable_for_new_orders


def test_stale_quote_in_session() -> None:
    r = evaluate_quote(q(IN_SESSION - timedelta(seconds=11)), IN_SESSION, CAL)
    assert r.status is FreshnessStatus.STALE
    assert not r.status.usable_for_new_orders


def test_policy_threshold_is_configurable() -> None:
    policy = FreshnessPolicy(intraday_max_age=timedelta(seconds=30))
    r = evaluate_quote(q(IN_SESSION - timedelta(seconds=20)), IN_SESSION, CAL, policy)
    assert r.status is FreshnessStatus.FRESH


def test_missing_and_invalid_quotes() -> None:
    assert evaluate_quote(None, IN_SESSION, CAL).status is FreshnessStatus.MISSING
    future = q(IN_SESSION + timedelta(minutes=1))
    assert evaluate_quote(future, IN_SESSION, CAL).status is FreshnessStatus.INVALID


def test_after_close_quote_from_today_is_session_closed() -> None:
    r = evaluate_quote(q(datetime(2026, 10, 30, 15, 29, 59, tzinfo=EXCHANGE_TZ)), AFTER_CLOSE, CAL)
    assert r.status is FreshnessStatus.SESSION_CLOSED
    assert not r.status.usable_for_new_orders


def test_weekend_quote_from_friday_is_session_closed_but_thursday_is_stale() -> None:
    fri = q(datetime(2026, 10, 30, 15, 29, tzinfo=EXCHANGE_TZ))
    thu = q(datetime(2026, 10, 29, 15, 29, tzinfo=EXCHANGE_TZ))
    assert evaluate_quote(fri, SATURDAY, CAL).status is FreshnessStatus.SESSION_CLOSED
    assert evaluate_quote(thu, SATURDAY, CAL).status is FreshnessStatus.STALE


# ------------------------------------------------------------------ daily freshness
def test_daily_freshness() -> None:
    thu_open = datetime(2026, 10, 29, 9, 15, tzinfo=EXCHANGE_TZ)
    fri_open = datetime(2026, 10, 30, 9, 15, tzinfo=EXCHANGE_TZ)
    # During Friday's session the last completed session is Thursday.
    assert evaluate_daily(thu_open, IN_SESSION, CAL).status is FreshnessStatus.FRESH
    # After Friday's close, Friday's bar is required.
    assert evaluate_daily(thu_open, AFTER_CLOSE, CAL).status is FreshnessStatus.STALE
    assert evaluate_daily(fri_open, SATURDAY, CAL).status is FreshnessStatus.FRESH
    assert evaluate_daily(None, SATURDAY, CAL).status is FreshnessStatus.MISSING
