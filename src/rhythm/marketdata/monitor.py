"""Freshness monitor: evaluates instruments per horizon and records quality transitions."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import Clock
from rhythm.core.market import Horizon, InstrumentKey
from rhythm.marketdata.bars import latest_bar
from rhythm.marketdata.freshness import (
    FreshnessPolicy,
    FreshnessResult,
    FreshnessStatus,
    evaluate_daily,
    evaluate_quote,
)
from rhythm.marketdata.provider import MarketDataProvider, MarketDataUnavailableError
from rhythm.marketdata.quality import QualityKind, Severity, record_quality_event
from rhythm.marketdata.types import Interval

_BAD = {
    FreshnessStatus.STALE: (QualityKind.STALE, Severity.WARNING),
    FreshnessStatus.MISSING: (QualityKind.MISSING, Severity.WARNING),
    FreshnessStatus.INVALID: (QualityKind.INVALID_QUOTE, Severity.CRITICAL),
    FreshnessStatus.ERROR: (QualityKind.PROVIDER_ERROR, Severity.CRITICAL),
}


@dataclass(frozen=True)
class InstrumentFreshness:
    key: InstrumentKey
    horizon: Horizon
    result: FreshnessResult
    source: str
    is_mock: bool


class FreshnessMonitor:
    def __init__(
        self,
        *,
        provider: MarketDataProvider | None,
        calendar: SessionCalendar,
        clock: Clock,
        policy: FreshnessPolicy | None = None,
    ) -> None:
        self.provider = provider
        self.calendar = calendar
        self.clock = clock
        self.policy = policy or FreshnessPolicy()
        self._last: dict[tuple[InstrumentKey, Horizon], FreshnessStatus] = {}
        self._lock = threading.Lock()

    def _evaluate(
        self, session: Session, key: InstrumentKey, horizon: Horizon
    ) -> InstrumentFreshness:
        now = self.clock.now()
        if horizon is Horizon.INTRADAY:
            if self.provider is None:
                res = FreshnessResult(
                    FreshnessStatus.MISSING, reasons=["no market data provider configured"]
                )
                return InstrumentFreshness(key, horizon, res, "none", False)
            try:
                quote = self.provider.latest_quote(key)
            except MarketDataUnavailableError as exc:
                res = FreshnessResult(FreshnessStatus.ERROR, reasons=[str(exc)])
            else:
                res = evaluate_quote(quote, now, self.calendar, self.policy)
            return InstrumentFreshness(
                key, horizon, res, self.provider.name, self.provider.origin.value == "MOCK"
            )
        latest = latest_bar(session, key, Interval.D1)
        res = evaluate_daily(None if latest is None else latest.bar_start, now, self.calendar)
        if latest is None:
            return InstrumentFreshness(key, horizon, res, "db", False)
        return InstrumentFreshness(key, horizon, res, f"db:{latest.source}", latest.is_mock)

    def check(
        self, session: Session, keys: Sequence[InstrumentKey], horizon: Horizon
    ) -> list[InstrumentFreshness]:
        results = [self._evaluate(session, k, horizon) for k in keys]
        for r in results:
            self._record_transition(session, r)
        return results

    def _record_transition(self, session: Session, r: InstrumentFreshness) -> None:
        status = r.result.status
        with self._lock:
            prev = self._last.get((r.key, r.horizon))
            self._last[(r.key, r.horizon)] = status
        if status in _BAD and prev != status:
            kind, severity = _BAD[status]
            record_quality_event(
                session,
                self.clock,
                source=r.source,
                kind=kind,
                severity=severity,
                key=r.key,
                detail={"horizon": r.horizon.value, "reasons": r.result.reasons, "previous": prev},
            )
        elif status not in _BAD and prev in _BAD:
            record_quality_event(
                session,
                self.clock,
                source=r.source,
                kind=QualityKind.RECOVERED,
                severity=Severity.INFO,
                key=r.key,
                detail={"horizon": r.horizon.value, "previous": prev, "now": status.value},
            )
