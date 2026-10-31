"""Market-data value types. Every datum carries its source and timestamps (provenance)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from rhythm.core.clock import ensure_utc
from rhythm.core.market import DataOrigin, InstrumentKey


class Interval(StrEnum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M10 = "10m"
    M15 = "15m"
    M30 = "30m"
    M60 = "60m"
    M90 = "90m"
    D1 = "1d"
    W1 = "1w"
    MO1 = "1mo"

    @property
    def is_intraday(self) -> bool:
        return self.value.endswith("m") and self is not Interval.MO1

    @property
    def duration(self) -> timedelta | None:
        if self.is_intraday:
            return timedelta(minutes=int(self.value[:-1]))
        if self is Interval.D1:
            return timedelta(days=1)
        if self is Interval.W1:
            return timedelta(weeks=1)
        return None  # calendar months are variable


@dataclass(frozen=True)
class Quote:
    key: InstrumentKey
    ltp: Decimal
    source: DataOrigin
    source_ts: datetime  # exchange/vendor timestamp of the last trade or update
    received_ts: datetime  # when we received it
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None
    lower_circuit: Decimal | None = None
    upper_circuit: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ts", ensure_utc(self.source_ts))
        object.__setattr__(self, "received_ts", ensure_utc(self.received_ts))

    @property
    def is_mock(self) -> bool:
        return self.source is DataOrigin.MOCK


@dataclass(frozen=True)
class Bar:
    key: InstrumentKey
    interval: Interval
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: DataOrigin
    received_ts: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start))
        object.__setattr__(self, "received_ts", ensure_utc(self.received_ts))

    @property
    def is_mock(self) -> bool:
        return self.source is DataOrigin.MOCK
