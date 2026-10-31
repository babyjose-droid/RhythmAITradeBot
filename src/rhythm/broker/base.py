"""Read-only broker interface.

There are deliberately NO order methods here. Order submission will be a separate,
gated interface that does not exist before the Phase 10 live-readiness review.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from rhythm.core.errors import RhythmError
from rhythm.core.market import DataOrigin, Exchange
from rhythm.instruments.types import ScripRecord


class BrokerError(RhythmError):
    pass


class BrokerAuthError(BrokerError):
    """Session missing/expired (Sharekhan: HTTP 403, token valid 24 h). Requires a human login."""


class BrokerRateLimitedError(BrokerError):
    """HTTP 429."""


class BrokerUnavailableError(BrokerError):
    """Broker/RMS down (Sharekhan: HTTP 502/500) or network failure."""


class BrokerResponseError(BrokerError):
    """Response did not match the expected (documented) shape."""


@dataclass(frozen=True)
class BrokerProfile:
    broker: str
    account_ref: str  # masked identifier, never the raw customer ID
    exchanges: tuple[str, ...]
    origin: DataOrigin
    as_of: datetime


@dataclass(frozen=True)
class FundsSnapshot:
    exchange: Exchange
    cash_balance: Decimal
    origin: DataOrigin
    as_of: datetime
    raw_fields: tuple[tuple[str, str], ...] = ()  # other reported fields, uninterpreted


@dataclass(frozen=True)
class HoldingRecord:
    exchange: Exchange
    symbol: str
    quantity: int
    origin: DataOrigin
    as_of: datetime
    isin: str | None = None
    average_price: Decimal | None = None


@dataclass(frozen=True)
class PositionRecord:
    exchange: Exchange
    symbol: str
    product: str
    net_quantity: int
    origin: DataOrigin
    as_of: datetime
    average_price: Decimal | None = None


@dataclass(frozen=True)
class BrokerOrderRecord:
    """An order as reported by the broker. Status strings are kept verbatim (enum unknown)."""

    broker_order_id: str
    exchange: Exchange
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    status: str
    origin: DataOrigin
    as_of: datetime
    price: Decimal | None = None
    average_fill_price: Decimal | None = None


@runtime_checkable
class BrokerReadAdapter(Protocol):
    name: str
    origin: DataOrigin

    def get_profile(self) -> BrokerProfile: ...
    def get_funds(self, exchange: Exchange) -> FundsSnapshot: ...
    def get_holdings(self) -> Sequence[HoldingRecord]: ...
    def get_positions(self) -> Sequence[PositionRecord]: ...
    def get_day_orders(self) -> Sequence[BrokerOrderRecord]: ...
    def get_scrip_master(self, exchange: Exchange) -> Sequence[ScripRecord]: ...
