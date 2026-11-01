"""Synthetic bar builders for Phase 3 tests (clearly fake prices)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey
from rhythm.execution.runner import RiskEnvironment
from rhythm.marketdata.types import Bar, Interval
from rhythm.risk.engine import InstrumentFacts

KEY = InstrumentKey(Exchange.NSE, "MOCKA")
DAY = date(2026, 10, 30)  # Friday


def t(hh: int, mm: int, d: date = DAY) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=EXCHANGE_TZ)


def bar(
    start: datetime,
    o: str,
    h: str,
    lo: str,
    c: str,
    v: int = 1000,
    key: InstrumentKey = KEY,
) -> Bar:
    return Bar(
        key=key,
        interval=Interval.M5,
        start=start,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=v,
        source=DataOrigin.MOCK,
        received_ts=start + timedelta(minutes=5),
    )


def opening_range(d: date = DAY, key: InstrumentKey = KEY) -> list[Bar]:
    """09:15-09:30: OR high 101, OR low 100."""
    return [
        bar(t(9, 15, d), "100.2", "101", "100", "100.5", key=key),
        bar(t(9, 20, d), "100.5", "100.8", "100.1", "100.3", key=key),
        bar(t(9, 25, d), "100.3", "100.9", "100.2", "100.4", key=key),
    ]


def breakout_day(d: date = DAY, key: InstrumentKey = KEY) -> list[Bar]:
    """OR then a 09:30 bar closing above OR high on strong volume -> entry signal at 09:35."""
    return [*opening_range(d, key), bar(t(9, 30, d), "100.5", "102", "100.4", "101.8", 5000, key)]


def facts(lot: int = 1, tick: str = "0.05", itype: str = "EQ") -> InstrumentFacts:
    return InstrumentFacts(
        active=True, instrument_type=itype, lot_size=lot, tick_size=Decimal(tick)
    )


def sim_env(*keys: InstrumentKey, capital: Decimal = Decimal(1_000_000)) -> RiskEnvironment:
    """A backtest-style risk environment: book is the truth, MOCK-style instrument facts."""
    return RiskEnvironment(
        mode="BACKTEST",
        book_is_truth=True,
        ledger_allocation=capital,
        instruments={k: facts() for k in (keys or (KEY,))},
    )
