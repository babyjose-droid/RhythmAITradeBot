"""Deterministic MOCK market data with failure injection — for tests and local development.

Every quote and bar is labelled ``DataOrigin.MOCK``. Prices are a seeded synthetic walk and
bear no relation to any real instrument. Never use this provider in staging/production.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, Clock, SystemClock, ensure_utc
from rhythm.core.market import DataOrigin, InstrumentKey, round_to_tick
from rhythm.marketdata.provider import MarketDataUnavailableError
from rhythm.marketdata.types import Bar, Interval, Quote

TICK = Decimal("0.05")


@dataclass
class MockScenario:
    """Failure injection. Keys are InstrumentKeys."""

    stale: dict[InstrumentKey, timedelta] = field(default_factory=dict)  # quote lags by this
    missing: set[InstrumentKey] = field(default_factory=set)  # no data at all
    errors: set[InstrumentKey] = field(default_factory=set)  # provider raises
    future: set[InstrumentKey] = field(default_factory=set)  # timestamp ahead of now
    crossed: set[InstrumentKey] = field(default_factory=set)  # bid > ask
    outage: bool = False  # every call raises


def _stable_int(*parts: object) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big")


class MockMarketDataProvider:
    name = "mock"
    origin = DataOrigin.MOCK

    def __init__(
        self,
        *,
        seed: int = 7,
        clock: Clock | None = None,
        calendar: SessionCalendar | None = None,
        scenario: MockScenario | None = None,
    ) -> None:
        self.seed = seed
        self.clock = clock or SystemClock()
        self.calendar = calendar or SessionCalendar()
        self.scenario = scenario or MockScenario()

    # ------------------------------------------------------------------ price model
    def _price_at(self, key: InstrumentKey, at: datetime) -> Decimal:
        base = 50 + _stable_int(self.seed, key) % 1950
        minutes = int(ensure_utc(at).timestamp() // 60)
        wave = 0.03 * math.sin(minutes / 390.0 + (_stable_int(key) % 100))
        rng = random.Random(_stable_int(self.seed, key, minutes))  # noqa: S311 — not crypto
        noise = rng.uniform(-0.004, 0.004)
        return round_to_tick(Decimal(str(base * (1 + wave + noise))), TICK)

    def _check(self, key: InstrumentKey) -> None:
        if self.scenario.outage or key in self.scenario.errors:
            raise MarketDataUnavailableError(f"mock provider error for {key}")

    # ------------------------------------------------------------------ interface
    def latest_quote(self, key: InstrumentKey) -> Quote | None:
        self._check(key)
        if key in self.scenario.missing:
            return None
        now = ensure_utc(self.clock.now())
        if self.calendar.is_session_open(now):
            ts = now.replace(microsecond=0) - timedelta(seconds=1)
        else:
            last = self.calendar.last_completed_session(now)
            ts = self.calendar.session_close(last).astimezone(now.tzinfo) - timedelta(seconds=1)
        ts -= self.scenario.stale.get(key, timedelta(0))
        if key in self.scenario.future:
            ts = now + timedelta(minutes=5)
        ltp = self._price_at(key, ts)
        bid, ask = ltp - TICK, ltp + TICK
        if key in self.scenario.crossed:
            bid, ask = ask + TICK, bid
        return Quote(
            key=key,
            ltp=ltp,
            bid=bid,
            ask=ask,
            volume=_stable_int(self.seed, key, ts.date()) % 5_000_000,
            lower_circuit=round_to_tick(ltp * Decimal("0.9"), TICK),
            upper_circuit=round_to_tick(ltp * Decimal("1.1"), TICK),
            source=DataOrigin.MOCK,
            source_ts=ts,
            received_ts=now,
        )

    def bars(
        self, key: InstrumentKey, interval: Interval, start: datetime, end: datetime
    ) -> Sequence[Bar]:
        self._check(key)
        if key in self.scenario.missing:
            return []
        start, end = ensure_utc(start), ensure_utc(end)
        now = ensure_utc(self.clock.now())
        end = min(end, now)
        if interval is Interval.D1:
            return self._daily_bars(key, start, end, now)
        if interval.is_intraday and interval.duration is not None:
            return self._intraday_bars(key, interval, interval.duration, start, end, now)
        raise MarketDataUnavailableError(f"mock provider does not generate {interval.value} bars")

    def _make_bar(
        self, key: InstrumentKey, interval: Interval, s: datetime, e: datetime, now: datetime
    ) -> Bar:
        points = [self._price_at(key, s + (e - s) * i / 4) for i in range(5)]
        return Bar(
            key=key,
            interval=interval,
            start=s,
            open=points[0],
            high=max(points),
            low=min(points),
            close=points[-1],
            volume=_stable_int(self.seed, key, s) % 1_000_000,
            source=DataOrigin.MOCK,
            received_ts=now,
        )

    def _daily_bars(
        self, key: InstrumentKey, start: datetime, end: datetime, now: datetime
    ) -> list[Bar]:
        out: list[Bar] = []
        d = start.astimezone(EXCHANGE_TZ).date()
        while True:
            s = self.calendar.session_open(d)
            if s >= end:
                break
            close = self.calendar.session_close(d)
            if self.calendar.is_trading_day(d) and s >= start and close <= now:
                out.append(self._make_bar(key, Interval.D1, s, close, now))
            d += timedelta(days=1)
        return out

    def _intraday_bars(
        self,
        key: InstrumentKey,
        interval: Interval,
        step: timedelta,
        start: datetime,
        end: datetime,
        now: datetime,
    ) -> list[Bar]:
        out: list[Bar] = []
        d = start.astimezone(EXCHANGE_TZ).date()
        while self.calendar.session_open(d) < end:
            if self.calendar.is_trading_day(d):
                s = self.calendar.session_open(d)
                close = self.calendar.session_close(d)
                while s < close:
                    e = min(s + step, close)
                    if s >= start and s < end and e <= now:
                        out.append(self._make_bar(key, interval, s, e, now))
                    s = e
            d += timedelta(days=1)
        return out
