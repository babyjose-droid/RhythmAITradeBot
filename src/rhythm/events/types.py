"""Corporate events (results, dividends, splits, ...) as point-in-time facts.

Every event records when it became known (``known_at``). Strategies and the risk engine only
ever see events with ``known_at <= decision time``, so a backtest cannot use an announcement
before it was made.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from rhythm.core.clock import ensure_utc
from rhythm.core.market import DataOrigin, InstrumentKey


class EventKind(StrEnum):
    RESULTS = "RESULTS"
    DIVIDEND = "DIVIDEND"
    SPLIT = "SPLIT"
    BONUS = "BONUS"
    AGM = "AGM"
    BOARD_MEETING = "BOARD_MEETING"
    OTHER = "OTHER"


@dataclass(frozen=True)
class CorporateEvent:
    key: InstrumentKey
    kind: EventKind
    event_date: date  # exchange-local date of the event
    known_at: datetime  # when the event became public (UTC)
    source: DataOrigin | str
    source_ref: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_at", ensure_utc(self.known_at))

    @property
    def is_mock(self) -> bool:
        return str(self.source) == DataOrigin.MOCK.value


class EventProvider(Protocol):
    name: str
    origin: DataOrigin

    def events(self, key: InstrumentKey, start: date, end: date) -> Sequence[CorporateEvent]: ...


def known_upcoming(
    events: Sequence[CorporateEvent], key: InstrumentKey, as_of: datetime, on_or_after: date
) -> list[CorporateEvent]:
    """Events for ``key`` already public at ``as_of`` and dated on/after ``on_or_after``."""
    as_of = ensure_utc(as_of)
    return sorted(
        (e for e in events if e.key == key and e.known_at <= as_of and e.event_date >= on_or_after),
        key=lambda e: (e.event_date, e.kind.value),
    )
