"""Instrument reference data as delivered by a source (broker scrip master or mock)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rhythm.core.market import DataOrigin, Exchange, InstrumentKey


@dataclass(frozen=True)
class ScripRecord:
    exchange: Exchange
    symbol: str
    source: DataOrigin
    source_code: str  # broker's own identifier (e.g. Sharekhan scripCode)
    instrument_type: str
    tick_size: Decimal
    lot_size: int
    isin: str | None = None
    name: str | None = None

    @property
    def key(self) -> InstrumentKey:
        return InstrumentKey(self.exchange, self.symbol)

    def attributes(self) -> dict[str, str | int | None]:
        """Attributes whose change opens a new point-in-time version of the instrument."""
        return {
            "source_code": self.source_code,
            "instrument_type": self.instrument_type,
            "tick_size": str(self.tick_size.normalize()),
            "lot_size": self.lot_size,
            "isin": self.isin,
            "name": self.name,
        }
