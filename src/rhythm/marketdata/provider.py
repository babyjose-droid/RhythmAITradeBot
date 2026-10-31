"""MarketDataProvider interface."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from rhythm.core.errors import RhythmError
from rhythm.core.market import DataOrigin, InstrumentKey
from rhythm.marketdata.types import Bar, Interval, Quote


class MarketDataUnavailableError(RhythmError):
    """The provider could not supply data (outage, auth expiry, rate limit...)."""


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str
    origin: DataOrigin

    def latest_quote(self, key: InstrumentKey) -> Quote | None:
        """Most recent quote, or None if the provider has none for this instrument."""
        ...

    def bars(
        self, key: InstrumentKey, interval: Interval, start: datetime, end: datetime
    ) -> Sequence[Bar]:
        """Bars with ``start <= bar.start < end``, oldest first."""
        ...
