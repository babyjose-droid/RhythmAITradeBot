"""Strategy contract. Strategies are pure, versioned and cannot see the future.

A strategy produces *signals* — candidate actions. A signal is never an order: it must pass
the risk gate and, where configured, approval before an order exists (PRODUCT_REQUIREMENTS §5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from rhythm.core.clock import ensure_utc
from rhythm.core.errors import RhythmError
from rhythm.core.market import Horizon, InstrumentKey
from rhythm.marketdata.types import Bar


class LookAheadError(RhythmError):
    """A strategy context contained data from after the decision time."""


class SignalType(StrEnum):
    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"


class DecisionState(StrEnum):
    """Recommendation states. Never proof that an order was sent or filled."""

    BUY_CANDIDATE = "BUY_CANDIDATE"
    ADD_CANDIDATE = "ADD_CANDIDATE"
    HOLD = "HOLD"
    REDUCE_CANDIDATE = "REDUCE_CANDIDATE"
    SELL_CANDIDATE = "SELL_CANDIDATE"
    EXIT_CANDIDATE = "EXIT_CANDIDATE"
    NO_ACTION = "NO_ACTION"
    REJECTED_BY_RISK = "REJECTED_BY_RISK"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


@dataclass(frozen=True)
class Signal:
    key: InstrumentKey
    horizon: Horizon
    type: SignalType
    at: datetime  # decision time (close of the last bar used)
    ref_price: Decimal  # last close — reference only; fills happen later
    strategy: str
    version: str
    stop: Decimal | None = None
    target: Decimal | None = None
    reasons: tuple[str, ...] = ()
    # Multi-day strategies state *why* they enter and what would prove them wrong.
    thesis: Mapping[str, Any] | None = None

    @property
    def decision(self) -> DecisionState:
        return (
            DecisionState.BUY_CANDIDATE
            if self.type is SignalType.ENTER_LONG
            else DecisionState.EXIT_CANDIDATE
        )


@dataclass(frozen=True)
class PositionView:
    key: InstrumentKey
    quantity: int
    average_price: Decimal
    opened_at: datetime


@dataclass(frozen=True)
class EventView:
    """A corporate event as known at decision time (``known_at <= now``)."""

    kind: str
    event_date: date
    known_at: datetime


def default_bar_end(b: Bar) -> datetime:
    dur = b.interval.duration
    return b.start + dur if dur is not None else b.start


@dataclass(frozen=True)
class StrategyContext:
    """Point-in-time view handed to a strategy. Construction fails on any future data."""

    key: InstrumentKey
    now: datetime
    bars: tuple[Bar, ...]  # completed bars only, oldest first
    position: PositionView | None = None
    entries_today: int = 0
    events: tuple[EventView, ...] = ()
    # Daily bars end at the session close, not 24 h after the open: the runner supplies it.
    bar_end: Callable[[Bar], datetime] = default_bar_end

    def __post_init__(self) -> None:
        now = ensure_utc(self.now)
        object.__setattr__(self, "now", now)
        for e in self.events:
            if ensure_utc(e.known_at) > now:
                raise LookAheadError(f"event {e.kind} {e.event_date} was not known at {now}")
        prev: datetime | None = None
        for b in self.bars:
            end = self.bar_end(b)
            if end > now:
                raise LookAheadError(f"bar {b.start.isoformat()} ends after decision time")
            if b.key != self.key:
                raise LookAheadError("context contains another instrument's bars")
            if prev is not None and b.start <= prev:
                raise LookAheadError("bars must be strictly increasing in time")
            prev = b.start


@dataclass(frozen=True)
class Evaluation:
    signals: list[Signal] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)  # why / why not (for audit)


class Strategy(Protocol):
    name: str
    version: str
    horizon: Horizon

    @property
    def params(self) -> dict[str, Any]: ...

    def evaluate(self, ctx: StrategyContext) -> Evaluation: ...


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


def dataclass_params(obj: Any) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(obj).items()}


def session_vwap(bars: Sequence[Bar]) -> Decimal | None:
    """Volume-weighted typical price over the given bars (None if no volume)."""
    vol = sum(b.volume for b in bars)
    if vol <= 0:
        return None
    pv = sum((((b.high + b.low + b.close) / 3) * b.volume for b in bars), Decimal(0))
    return pv / Decimal(vol)
