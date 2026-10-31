"""Data freshness rules per horizon. Pure functions; fail closed (anything uncertain is STALE).

* Intraday uses live quotes. During the session a quote older than ``intraday_max_age`` is
  STALE. Outside the session a quote is SESSION_CLOSED if it belongs to the latest session,
  otherwise STALE.
* Swing and long-term use daily bars: the latest stored daily bar must be for the most
  recently *completed* session.

Defaults are illustrative paper-test settings, not recommendations (RISK_MANAGEMENT.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, ensure_utc
from rhythm.marketdata.types import Quote
from rhythm.marketdata.validation import DEFAULT_FUTURE_TOLERANCE, quote_issues


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    SESSION_CLOSED = "SESSION_CLOSED"  # last session's data; acceptable outside market hours
    STALE = "STALE"
    MISSING = "MISSING"
    INVALID = "INVALID"
    ERROR = "ERROR"

    @property
    def usable_for_new_orders(self) -> bool:
        """Only FRESH data may ever support a new intraday order (enforced by the risk engine)."""
        return self is FreshnessStatus.FRESH


@dataclass(frozen=True)
class FreshnessPolicy:
    intraday_max_age: timedelta = timedelta(seconds=10)
    future_tolerance: timedelta = DEFAULT_FUTURE_TOLERANCE


@dataclass(frozen=True)
class FreshnessResult:
    status: FreshnessStatus
    age_seconds: float | None = None
    as_of: datetime | None = None
    reasons: list[str] = field(default_factory=list)


def evaluate_quote(
    quote: Quote | None,
    now: datetime,
    calendar: SessionCalendar,
    policy: FreshnessPolicy | None = None,
) -> FreshnessResult:
    policy = policy or FreshnessPolicy()
    now = ensure_utc(now)
    if quote is None:
        return FreshnessResult(FreshnessStatus.MISSING, reasons=["no quote"])
    issues = quote_issues(quote, now, future_tolerance=policy.future_tolerance)
    age = (now - quote.source_ts).total_seconds()
    if issues:
        return FreshnessResult(FreshnessStatus.INVALID, age, quote.source_ts, issues)
    if calendar.is_session_open(now):
        if age <= policy.intraday_max_age.total_seconds():
            return FreshnessResult(FreshnessStatus.FRESH, age, quote.source_ts)
        return FreshnessResult(
            FreshnessStatus.STALE,
            age,
            quote.source_ts,
            [f"quote age {age:.1f}s > {policy.intraday_max_age.total_seconds():.0f}s"],
        )
    last_open = calendar.current_or_last_session_open(now)
    if quote.source_ts >= last_open:
        return FreshnessResult(FreshnessStatus.SESSION_CLOSED, age, quote.source_ts)
    return FreshnessResult(
        FreshnessStatus.STALE, age, quote.source_ts, ["quote predates the latest session"]
    )


def evaluate_daily(
    latest_bar_start: datetime | None, now: datetime, calendar: SessionCalendar
) -> FreshnessResult:
    if latest_bar_start is None:
        return FreshnessResult(FreshnessStatus.MISSING, reasons=["no daily bars stored"])
    now = ensure_utc(now)
    bar_day: date = ensure_utc(latest_bar_start).astimezone(EXCHANGE_TZ).date()
    expected = calendar.last_completed_session(now)
    age = (now - ensure_utc(latest_bar_start)).total_seconds()
    if bar_day >= expected:
        return FreshnessResult(FreshnessStatus.FRESH, age, latest_bar_start)
    return FreshnessResult(
        FreshnessStatus.STALE,
        age,
        latest_bar_start,
        [f"latest daily bar {bar_day.isoformat()} < expected {expected.isoformat()}"],
    )
