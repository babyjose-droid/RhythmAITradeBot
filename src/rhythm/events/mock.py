"""Deterministic MOCK corporate events: one RESULTS date per quarter per instrument.

Dates are synthetic (hash-based) and labelled MOCK. Each is "announced" 21 days ahead.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import DataOrigin, InstrumentKey
from rhythm.events.types import CorporateEvent, EventKind

ANNOUNCE_DAYS = 21


def _h(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


class MockEventProvider:
    name = "mock"
    origin = DataOrigin.MOCK

    def __init__(self, seed: int = 11) -> None:
        self.seed = seed

    def _results_date(self, key: InstrumentKey, year: int, quarter: int) -> date:
        # Results season: weeks 2-6 after quarter end; weekday only.
        q_end_month = quarter * 3
        base = date(year, q_end_month, 28) + timedelta(days=14)
        d = base + timedelta(days=_h(self.seed, key, year, quarter) % 28)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d

    def events(self, key: InstrumentKey, start: date, end: date) -> Sequence[CorporateEvent]:
        out: list[CorporateEvent] = []
        for year in range(start.year - 1, end.year + 1):
            for q in (1, 2, 3, 4):
                d = self._results_date(key, year, q)
                if start <= d <= end:
                    known = datetime.combine(
                        d - timedelta(days=ANNOUNCE_DAYS), time(18, 0), tzinfo=EXCHANGE_TZ
                    )
                    out.append(
                        CorporateEvent(
                            key=key,
                            kind=EventKind.RESULTS,
                            event_date=d,
                            known_at=known,
                            source=DataOrigin.MOCK,
                            source_ref=f"mock:{key}:{year}Q{q}",
                            note="synthetic results date (MOCK)",
                        )
                    )
        return sorted(out, key=lambda e: e.event_date)
