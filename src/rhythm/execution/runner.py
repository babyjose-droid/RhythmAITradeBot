"""Intraday event runner shared by the backtester and the paper-trading service.

Per completed bar, in this order (no look-ahead):
1. resting orders try to fill against the new bar (decisions from earlier bars);
2. the strategy evaluates using bars up to and including this bar;
3. each signal goes through the risk gate; accepted signals become PAPER orders that can
   only fill from the next bar;
4. protective stop/target orders are kept in sync with the position (OCO);
5. at the session's last bar any remaining position is squared off and orders expire.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import InstrumentKey
from rhythm.execution.book import Fill, PaperBook
from rhythm.execution.costs import Side
from rhythm.execution.fills import OrderType
from rhythm.marketdata.types import Bar
from rhythm.orders.state import OrderState
from rhythm.risk import baseline
from rhythm.strategies.base import (
    DecisionState,
    PositionView,
    Signal,
    SignalType,
    Strategy,
    StrategyContext,
)

MAX_HISTORY = 400


@dataclass(frozen=True)
class SignalRecord:
    signal: Signal
    decision: DecisionState
    risk: baseline.RiskDecision
    order_id: str | None
    diagnostics: dict[str, Any]


@dataclass
class IntradayRunner:
    strategy: Strategy
    book: PaperBook
    calendar: SessionCalendar
    limits: baseline.BaselineRiskLimits = field(default_factory=baseline.BaselineRiskLimits)
    lot_sizes: dict[InstrumentKey, int] = field(default_factory=dict)
    history: dict[InstrumentKey, list[Bar]] = field(default_factory=lambda: defaultdict(list))
    marks: dict[InstrumentKey, Decimal] = field(default_factory=dict)
    entries: dict[tuple[InstrumentKey, date], int] = field(default_factory=lambda: defaultdict(int))
    day_start_equity: dict[date, Decimal] = field(default_factory=dict)

    def _day(self, bar: Bar) -> date:
        return bar.start.astimezone(EXCHANGE_TZ).date()

    def on_bar(self, bar: Bar, *, data_fresh: bool = True) -> tuple[list[Fill], list[SignalRecord]]:
        key, day = bar.key, self._day(bar)
        dur = bar.interval.duration
        if dur is None:
            raise ValueError("intraday runner needs fixed-duration bars")
        end = bar.start + dur
        self.day_start_equity.setdefault(day, self.book.equity(self.marks))

        fills = self.book.on_bar(bar)  # 1. earlier decisions fill on this bar
        if any(self.book.orders[f.order_id].purpose == "entry" for f in fills):
            # Conservative: the bar that filled the entry may also have hit the stop.
            self._sync_protection(key, bar.start)
            fills += self.book.on_bar(bar, purposes=frozenset({"stop"}))
        hist = self.history[key]
        hist.append(bar)
        del hist[:-MAX_HISTORY]
        self.marks[key] = bar.close

        pos = self.book.position(key)
        view = (
            PositionView(key, pos.quantity, pos.average_price, pos.opened_at or end)
            if pos.quantity > 0
            else None
        )
        today = tuple(b for b in hist if self._day(b) == day)
        ctx = StrategyContext(key, end, today, view, self.entries[(key, day)])
        evaluation = self.strategy.evaluate(ctx)  # 2.

        records = [
            self._handle(sig, evaluation.diagnostics, data_fresh) for sig in evaluation.signals
        ]
        self._sync_protection(key, end)  # 4.

        if end >= self.calendar.session_close(day):  # 5.
            if pos.quantity > 0:
                fill = self.book.force_close(key, bar.close, end, "session square-off")
                if fill is not None:
                    fills.append(fill)
            self.book.expire_working(end, key)
        return fills, records

    def _account_state(self, key: InstrumentKey, day: date) -> baseline.AccountState:
        equity = self.book.equity(self.marks)
        return baseline.AccountState(
            equity=equity,
            cash=self.book.cash,
            day_start_equity=self.day_start_equity.get(day, equity),
            open_orders=self.book.open_order_count(),
            open_positions=sum(1 for p in self.book.positions.values() if p.quantity > 0),
            position_quantity=self.book.position(key).quantity,
        )

    def _handle(self, sig: Signal, diag: dict[str, Any], fresh: bool) -> SignalRecord:
        key = sig.key
        day = sig.at.astimezone(EXCHANGE_TZ).date()
        decision = baseline.evaluate(
            sig,
            self._account_state(key, day),
            data_fresh=fresh,
            lot_size=self.lot_sizes.get(key, 1),
            limits=self.limits,
        )
        if not decision.accepted:
            return SignalRecord(sig, DecisionState.REJECTED_BY_RISK, decision, None, diag)
        idem = f"{sig.strategy}:{sig.version}:{key}:{sig.at.isoformat()}:{sig.type.value}"
        if sig.type is SignalType.ENTER_LONG:
            order, created = self.book.propose(
                key=key,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=decision.quantity,
                at=sig.at,
                idempotency_key=idem,
                strategy=sig.strategy,
                purpose="entry",
            )
            pos = self.book.position(key)
            pos.stop, pos.target = sig.stop, sig.target
            self.entries[(key, day)] += 1
        else:
            for o in self.book.working_orders(key):
                self.book.cancel(o, sig.at, "strategy exit")
            order, created = self.book.propose(
                key=key,
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=decision.quantity,
                at=sig.at,
                idempotency_key=idem,
                strategy=sig.strategy,
                purpose="exit",
            )
        if created:
            self.book.transition(order, OrderState.RISK_CHECKED, sig.at, **decision.as_dict())
            self.book.submit(order, sig.at)
        return SignalRecord(sig, sig.decision, decision, order.id, diag)

    def _sync_protection(self, key: InstrumentKey, at: datetime) -> None:
        """Keep one OCO stop+target pair sized to the open position."""
        pos = self.book.position(key)
        working = self.book.working_orders(key)
        if any(o.purpose == "exit" for o in working):
            return  # an explicit exit is pending; protection is not needed
        prot = [o for o in working if o.purpose in ("stop", "target")]
        if pos.quantity <= 0 or pos.stop is None:
            for o in prot:
                self.book.cancel(o, at, "no position")
            return
        if prot and all(o.remaining == pos.quantity for o in prot) and len(prot) == 2:
            return
        for o in prot:
            self.book.cancel(o, at, "resize protection")
        group = f"oco:{key}:{at.isoformat()}"
        legs: list[tuple[str, OrderType, Decimal | None, Decimal | None]] = [
            ("stop", OrderType.STOP, None, pos.stop)
        ]
        if pos.target is not None:
            legs.append(("target", OrderType.LIMIT, pos.target, None))
        for purpose, otype, limit, stop in legs:
            order, created = self.book.propose(
                key=key,
                side=Side.SELL,
                order_type=otype,
                quantity=pos.quantity,
                at=at,
                idempotency_key=f"{group}:{purpose}",
                limit_price=limit,
                stop_price=stop,
                strategy=self.strategy.name,
                purpose=purpose,
                oco_group=group,
            )
            if created:
                self.book.transition(order, OrderState.RISK_CHECKED, at, gate="risk-reducing exit")
                self.book.submit(order, at)
