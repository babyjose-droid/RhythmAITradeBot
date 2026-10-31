"""Deterministic sanity checks for incoming quotes and bars."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from rhythm.core.clock import ensure_utc
from rhythm.marketdata.types import Bar, Quote

DEFAULT_FUTURE_TOLERANCE = timedelta(seconds=2)


def bar_issues(
    bar: Bar, now: datetime, *, future_tolerance: timedelta = DEFAULT_FUTURE_TOLERANCE
) -> list[str]:
    issues: list[str] = []
    zero = Decimal(0)
    if min(bar.open, bar.high, bar.low, bar.close) <= zero:
        issues.append("non-positive price")
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        issues.append("OHLC inconsistent (low/high do not bound open/close)")
    if bar.low > bar.high:
        issues.append("low above high")
    if bar.volume < 0:
        issues.append("negative volume")
    if bar.start > ensure_utc(now) + future_tolerance:
        issues.append("bar starts in the future")
    return issues


def quote_issues(
    quote: Quote, now: datetime, *, future_tolerance: timedelta = DEFAULT_FUTURE_TOLERANCE
) -> list[str]:
    issues: list[str] = []
    if quote.ltp <= 0:
        issues.append("non-positive last traded price")
    if quote.bid is not None and quote.ask is not None and quote.bid > quote.ask:
        issues.append("crossed market (bid above ask)")
    if quote.volume is not None and quote.volume < 0:
        issues.append("negative volume")
    if (
        quote.lower_circuit is not None
        and quote.upper_circuit is not None
        and not quote.lower_circuit <= quote.ltp <= quote.upper_circuit
    ):
        issues.append("price outside circuit band")
    if quote.source_ts > ensure_utc(now) + future_tolerance:
        issues.append("source timestamp in the future")
    return issues
