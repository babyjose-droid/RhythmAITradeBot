"""Market vocabulary shared by every module: exchanges, horizons, instrument keys, prices."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-_.]{0,31}$")


class Exchange(StrEnum):
    """Cash-equity exchanges in MVP scope. Broker-specific codes live in the adapters."""

    NSE = "NSE"
    BSE = "BSE"


class Horizon(StrEnum):
    INTRADAY = "INTRADAY"
    SWING = "SWING"
    LONG_TERM = "LONG_TERM"


class DataOrigin(StrEnum):
    """Where a datum came from. MOCK data must never be presented as real."""

    MOCK = "MOCK"
    SHAREKHAN = "SHAREKHAN"
    VENDOR = "VENDOR"


@dataclass(frozen=True, order=True)
class InstrumentKey:
    exchange: Exchange
    symbol: str

    def __post_init__(self) -> None:
        if not _SYMBOL.match(self.symbol):
            raise ValueError(f"invalid trading symbol {self.symbol!r}")

    def __str__(self) -> str:
        return f"{self.exchange.value}:{self.symbol}"

    @classmethod
    def parse(cls, text: str) -> InstrumentKey:
        exchange, _, symbol = text.partition(":")
        if not symbol:
            raise ValueError("expected EXCHANGE:SYMBOL")
        return cls(Exchange(exchange.upper()), symbol.upper())


def to_decimal(value: object) -> Decimal:
    """Convert a price-like value to Decimal without float artefacts (floats go via str)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a price")
    if isinstance(value, int | str):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    raise TypeError(f"cannot convert {type(value).__name__} to Decimal")


def round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        raise ValueError("tick size must be positive")
    return (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_EVEN) * tick


def is_tick_multiple(price: Decimal, tick: Decimal) -> bool:
    return tick > 0 and (price % tick) == 0
