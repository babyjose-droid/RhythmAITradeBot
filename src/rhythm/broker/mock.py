"""MOCK read-only broker with deterministic fixtures and failure injection.

All symbols are synthetic (prefixed ``MOCK``) so they can never be mistaken for real
instruments, and every record is labelled ``DataOrigin.MOCK``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from rhythm.broker.base import (
    BrokerAuthError,
    BrokerOrderRecord,
    BrokerProfile,
    BrokerRateLimitedError,
    BrokerUnavailableError,
    FundsSnapshot,
    HoldingRecord,
    PositionRecord,
)
from rhythm.core.clock import Clock, SystemClock
from rhythm.core.market import DataOrigin, Exchange
from rhythm.instruments.types import ScripRecord

MOCK_SYMBOLS: tuple[str, ...] = tuple(f"MOCK{c}" for c in "ABCDEFGHIJKLMNOPQRST")  # 20 names


def mock_scrip_master(
    exchange: Exchange = Exchange.NSE, symbols: Sequence[str] = MOCK_SYMBOLS
) -> list[ScripRecord]:
    return [
        ScripRecord(
            exchange=exchange,
            symbol=s,
            source=DataOrigin.MOCK,
            source_code=str(900_000 + i),
            instrument_type="EQ",
            tick_size=Decimal("0.05"),
            lot_size=1,
            isin=None,  # deliberately no ISIN: mock instruments have no real identifier
            name=f"Mock Company {s[4:]} (synthetic)",
        )
        for i, s in enumerate(symbols)
    ]


@dataclass
class MockBrokerScenario:
    auth_expired: bool = False
    rate_limited: bool = False
    unavailable: bool = False
    scrip_master_override: list[ScripRecord] | None = None
    holdings: list[tuple[str, int]] = field(default_factory=lambda: [("MOCKA", 10), ("MOCKB", 5)])


class MockBrokerAdapter:
    name = "mock"
    origin = DataOrigin.MOCK

    def __init__(self, *, clock: Clock | None = None, scenario: MockBrokerScenario | None = None):
        self.clock = clock or SystemClock()
        self.scenario = scenario or MockBrokerScenario()

    def _check(self) -> None:
        if self.scenario.auth_expired:
            raise BrokerAuthError("mock: session expired — human login required")
        if self.scenario.rate_limited:
            raise BrokerRateLimitedError("mock: rate limited")
        if self.scenario.unavailable:
            raise BrokerUnavailableError("mock: broker unavailable")

    def get_profile(self) -> BrokerProfile:
        self._check()
        return BrokerProfile("mock", "******01", ("NC", "BC"), DataOrigin.MOCK, self.clock.now())

    def get_funds(self, exchange: Exchange) -> FundsSnapshot:
        self._check()
        return FundsSnapshot(exchange, Decimal("1000000.00"), DataOrigin.MOCK, self.clock.now())

    def get_holdings(self) -> Sequence[HoldingRecord]:
        self._check()
        now = self.clock.now()
        return [
            HoldingRecord(Exchange.NSE, s, q, DataOrigin.MOCK, now)
            for s, q in self.scenario.holdings
        ]

    def get_positions(self) -> Sequence[PositionRecord]:
        self._check()
        return []

    def get_day_orders(self) -> Sequence[BrokerOrderRecord]:
        self._check()
        return []

    def get_scrip_master(self, exchange: Exchange) -> Sequence[ScripRecord]:
        self._check()
        if self.scenario.scrip_master_override is not None:
            return list(self.scenario.scrip_master_override)
        return mock_scrip_master(exchange)
