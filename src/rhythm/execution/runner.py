"""Bar runners shared by the backtester and the paper-trading service.

``IntradayRunner`` (5-minute bars, flat by the close) and ``SwingRunner`` (daily bars,
multi-day positions, protection kept at the venue across days) share one decision path.


Per completed bar, in this order (no look-ahead):
1. resting orders try to fill against the new bar (decisions from earlier bars);
2. the strategy evaluates using bars up to and including this bar;
3. every signal becomes an ``OrderProposal`` and goes through the risk engine; accepted
   proposals become PAPER orders that can only fill from the next bar;
4. protective stop/target orders are kept in sync with the position (OCO) — they are
   proposals too and pass the risk engine as risk-reducing orders;
5. at the session's last bar any remaining position is squared off (simulated venue
   auto square-off) and orders expire.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Horizon, InstrumentKey
from rhythm.events.types import CorporateEvent
from rhythm.execution.book import Fill, Order, PaperBook
from rhythm.execution.costs import Product, Side
from rhythm.execution.fills import OrderType
from rhythm.fundamentals.types import FundamentalSnapshot, known_as_of
from rhythm.marketdata.types import Bar, Interval
from rhythm.orders.state import TERMINAL, OrderState
from rhythm.risk import engine
from rhythm.risk.engine import InstrumentFacts, Intent, OrderProposal, RiskDecision, RiskState
from rhythm.risk.policy import DEFAULT_POLICY, RiskPolicy
from rhythm.strategies.base import (
    DecisionState,
    EventView,
    PositionView,
    Signal,
    SignalType,
    Strategy,
    StrategyContext,
)

MAX_HISTORY = 400
ADV_LOOKBACK_SESSIONS = 20
PROTECTIVE = frozenset({"stop", "target", "square_off"})
RISK_REDUCING = PROTECTIVE | {"exit", "reduce"}  # not counted by R14


@dataclass
class RiskEnvironment:
    """Everything the runner cannot derive from its own book. Unknown -> fail closed."""

    mode: str = "BACKTEST"
    policy: RiskPolicy = DEFAULT_POLICY
    kill_switch_active: bool = False
    paused_strategies: frozenset[str] = frozenset()
    reconciled_at: datetime | None = None
    reconciliation_ok: bool = False
    book_is_truth: bool = False  # backtests: the in-memory book is the only record
    ledger_allocation: Decimal | None = None
    other_ledger_exposure: dict[InstrumentKey, Decimal] = field(default_factory=dict)
    instruments: dict[InstrumentKey, InstrumentFacts] = field(default_factory=dict)
    adv: dict[InstrumentKey, tuple[Decimal, str]] = field(default_factory=dict)
    # Swing (R18): known corporate events per instrument and the calendar's last sync time.
    events: dict[InstrumentKey, tuple[CorporateEvent, ...]] = field(default_factory=dict)
    event_synced_at: dict[InstrumentKey, datetime] = field(default_factory=dict)
    # Long-term: published fundamentals per instrument (point-in-time via known_at).
    fundamentals: dict[InstrumentKey, tuple[FundamentalSnapshot, ...]] = field(default_factory=dict)
    # Wall-clock time of a PAPER cycle (R3 compares reconciliation age against it).
    evaluated_at: datetime | None = None


@dataclass(frozen=True)
class RiskRecord:
    proposal: OrderProposal
    state: RiskState
    decision: RiskDecision
    order_id: str | None


@dataclass(frozen=True)
class Advice:
    """An advisory verdict on a risk-accepted entry/add. It can only allow or hold it."""

    allow: bool
    decision: DecisionState
    reason: str
    ref: int | None = None  # e.g. the stored research assessment id


# Called only for risk-increasing candidates the risk engine already accepted. It cannot
# change quantity, price or side: the runner uses only ``allow``.
Advisor = Callable[[Signal, dict[str, Any], RiskDecision], Advice]


@dataclass(frozen=True)
class SignalRecord:
    signal: Signal
    decision: DecisionState
    risk: RiskDecision
    order_id: str | None
    diagnostics: dict[str, Any]
    advice: Advice | None = None


@dataclass
class BaseRunner:
    strategy: Strategy
    book: PaperBook
    calendar: SessionCalendar
    env: RiskEnvironment = field(default_factory=RiskEnvironment)
    history: dict[InstrumentKey, list[Bar]] = field(default_factory=lambda: defaultdict(list))
    marks: dict[InstrumentKey, Decimal] = field(default_factory=dict)
    entries: dict[tuple[InstrumentKey, date], int] = field(default_factory=lambda: defaultdict(int))
    day_start_equity: dict[date, Decimal] = field(default_factory=dict)
    day_volume: dict[tuple[InstrumentKey, date], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    risk_log: list[RiskRecord] = field(default_factory=list)
    # Ledger-scoped day P&L (R8) = cash flow from this ledger's fills today
    # + change in the value of its positions since the start of the day.
    day_flow_base: dict[date, Decimal] = field(default_factory=dict)
    carried_flow: dict[date, Decimal] = field(default_factory=dict)
    day_start_value: dict[date, Decimal] = field(default_factory=dict)
    last_diagnostics: dict[InstrumentKey, dict[str, Any]] = field(default_factory=dict)
    signal_log: list[SignalRecord] = field(default_factory=list)
    advisor: Advisor | None = None  # PAPER research gate (Phase 7); None in backtests

    @property
    def horizon(self) -> Horizon:
        return self.book.horizon

    @property
    def product(self) -> Product:
        return Product.INTRADAY if self.horizon is Horizon.INTRADAY else Product.DELIVERY

    def _day(self, bar: Bar) -> date:
        return bar.start.astimezone(EXCHANGE_TZ).date()

    def positions_value(self) -> Decimal:
        return sum(
            (
                self.marks.get(k, p.average_price) * p.quantity
                for k, p in self.book.positions.items()
            ),
            Decimal(0),
        )

    def start_day(self, day: date) -> None:
        other = sum(self.env.other_ledger_exposure.values(), Decimal(0))
        self.day_start_equity.setdefault(day, self.book.equity(self.marks) + other)
        self.day_flow_base.setdefault(day, self.book.flow)
        self.day_start_value.setdefault(day, self.positions_value())

    def ledger_day_pnl(self, day: date) -> Decimal | None:
        if day not in self.day_start_value:
            return None
        flow = self.book.flow - self.day_flow_base.get(day, self.book.flow)
        return (
            self.carried_flow.get(day, Decimal(0))
            + flow
            + self.positions_value()
            - (self.day_start_value[day])
        )

    def day_flow(self, day: date) -> Decimal:
        """Today's ledger cash flow so far (persisted by the paper service between cycles)."""
        base = self.day_flow_base.get(day, self.book.flow)
        return self.carried_flow.get(day, Decimal(0)) + self.book.flow - base

    def _fill_then_protect(self, bar: Bar) -> list[Fill]:
        fills = self.book.on_bar(bar)  # earlier decisions fill on this bar
        if any(self.book.orders[f.order_id].purpose == "entry" for f in fills):
            # Conservative: the bar that filled the entry may also have hit the stop.
            self._sync_protection(bar.key, bar.start)
            fills += self.book.on_bar(bar, purposes=frozenset({"stop"}))
        return fills

    def _position_view(self, key: InstrumentKey, now: datetime) -> PositionView | None:
        pos = self.book.position(key)
        if pos.quantity <= 0:
            return None
        return PositionView(key, pos.quantity, pos.average_price, pos.opened_at or now)

    def on_bar(
        self, bar: Bar, *, quote_status: str = "FRESH"
    ) -> tuple[list[Fill], list[SignalRecord]]:
        raise NotImplementedError

    def _adv(self, key: InstrumentKey, day: date) -> tuple[Decimal | None, str]:
        raise NotImplementedError

    # ------------------------------------------------------------------ risk inputs
    def _open_risk_orders(self) -> list[Order]:
        return [
            o
            for o in self.book.orders.values()
            if o.state not in TERMINAL
            and o.state is not OrderState.RECONCILED
            and o.purpose not in RISK_REDUCING
        ]

    def risk_state(self, p: OrderProposal) -> RiskState:
        key, day = p.key, p.at.astimezone(EXCHANGE_TZ).date()
        equity = self.book.equity(self.marks)
        start = self.day_start_equity.get(day, equity)
        mark = self.marks.get(key, p.ref_price)
        open_risk = self._open_risk_orders()
        reserved = sum(
            (
                o.remaining * (o.limit_price or self.marks.get(o.key, Decimal(0)))
                for o in open_risk
                if o.side is Side.BUY
            ),
            Decimal(0),
        )
        ledger_exposure = sum(
            (
                self.marks.get(k, pp.average_price) * pp.quantity
                for k, pp in self.book.positions.items()
            ),
            Decimal(0),
        )
        pos = self.book.position(key)
        adv, adv_source = self._adv(key, day)
        env = self.env
        next_event = next(
            (
                e
                for e in sorted(env.events.get(key, ()), key=lambda e: e.event_date)
                if e.known_at <= p.at and e.event_date >= day
            ),
            None,
        )
        # Backtests/simulations: the in-memory book *is* the record, reconciled by definition.
        recon_at = (env.evaluated_at or p.at) if env.book_is_truth else env.reconciled_at
        recon_ok = True if env.book_is_truth else env.reconciliation_ok
        return RiskState(
            as_of=p.at,
            mode=env.mode,
            kill_switch_active=env.kill_switch_active,
            paused_strategies=env.paused_strategies,
            reconciled_at=recon_at,
            reconciliation_ok=recon_ok,
            instrument=env.instruments.get(key),
            quote_status=None,  # filled by caller
            equity=equity,
            day_start_equity=start,
            cash=self.book.cash,
            reserved_cash=reserved,
            strategy_day_pnl=self.ledger_day_pnl(day),
            ledger_allocation=env.ledger_allocation,
            ledger_exposure=ledger_exposure,
            ledger_position_qty=pos.quantity,
            consolidated_exposure=mark * pos.quantity
            + env.other_ledger_exposure.get(key, Decimal(0)),
            sector_exposure=None,
            avg_daily_volume=adv,
            adv_source=adv_source,
            open_orders_total=len(open_risk),
            open_orders_strategy=sum(1 for o in open_risk if o.strategy == p.strategy),
            orders_last_minute=sum(
                1
                for o in self.book.orders.values()
                if p.at - timedelta(minutes=1) < o.created_at <= p.at
            ),
            duplicate_idempotency_key=p.idempotency_key in self.book._by_idem,
            evaluated_at=env.evaluated_at,
            event_calendar_synced_at=env.event_synced_at.get(key)
            or (p.at if env.book_is_truth and key in env.events else None),
            days_to_next_event=None if next_event is None else (next_event.event_date - day).days,
            fundamentals_published_at=max(
                (f.known_at for f in env.fundamentals.get(key, ()) if f.known_at <= p.at),
                default=None,
            ),
            unknown_orders_on_instrument=sum(
                1
                for o in self.book.orders.values()
                if o.key == key and o.state is OrderState.UNKNOWN
            ),
        )

    def assess(self, p: OrderProposal, quote_status: str | None) -> tuple[RiskState, RiskDecision]:
        state = replace(self.risk_state(p), quote_status=quote_status)
        decision = engine.evaluate(p, state, self.env.policy, costs=self.book.cost_model)
        return state, decision

    # ------------------------------------------------------------------ signals
    def _weight_quantity(self, sig: Signal) -> int | None:
        """Shares needed to move this position to ``sig.target_weight`` of ledger capital."""
        cap = self.env.ledger_allocation
        if sig.target_weight is None or cap is None or sig.ref_price <= 0:
            return None
        facts = self.env.instruments.get(sig.key)
        lot = facts.lot_size if facts else 1
        target = engine.floor_lots(sig.target_weight * cap / sig.ref_price, lot)
        held = self.book.position(sig.key).quantity
        if sig.type in (SignalType.ENTER_LONG, SignalType.ADD_LONG):
            return max(0, target - held)
        return max(0, held - target)

    def _handle(
        self,
        sig: Signal,
        diag: dict[str, Any],
        quote_status: str,
        day: date,
        *,
        approved_by: str | None = None,
    ) -> SignalRecord:
        key = sig.key
        idem = f"{sig.strategy}:{sig.version}:{key}:{sig.at.isoformat()}:{sig.type.value}"
        buy = sig.type in (SignalType.ENTER_LONG, SignalType.ADD_LONG)
        purpose = {
            SignalType.ENTER_LONG: "entry",
            SignalType.ADD_LONG: "add",
            SignalType.REDUCE_LONG: "reduce",
            SignalType.EXIT_LONG: "exit",
        }[sig.type]
        sig = self._snap_to_tick(sig)
        held = self.book.position(key).quantity
        if sig.type is SignalType.EXIT_LONG:
            qty: int | None = held
        elif sig.target_weight is not None:
            qty = self._weight_quantity(sig)
        else:
            qty = None  # intraday/swing entries: the risk engine sizes them (R7)
        proposal = OrderProposal(
            key=key,
            horizon=sig.horizon,
            side=Side.BUY if buy else Side.SELL,
            intent=Intent.OPEN if buy else Intent.REDUCE,
            at=sig.at,
            ref_price=sig.ref_price,
            strategy=sig.strategy,
            idempotency_key=idem,
            stop=sig.stop if buy else None,
            quantity=qty,
            purpose=purpose,
            approved_by=approved_by,
        )
        state, decision = self.assess(proposal, quote_status)
        if not decision.accepted:
            self.risk_log.append(RiskRecord(proposal, state, decision, None))
            dstate = (
                DecisionState.REQUIRES_REVIEW
                if decision.outcome is engine.RiskOutcome.REVIEW
                else DecisionState.REJECTED_BY_RISK
            )
            return SignalRecord(sig, dstate, decision, None, diag)
        advice: Advice | None = None
        if buy and self.advisor is not None and approved_by is None:
            advice = self.advisor(sig, diag, decision)
            if not advice.allow:
                self.risk_log.append(RiskRecord(proposal, state, decision, None))
                return SignalRecord(sig, advice.decision, decision, None, diag, advice)
        if not buy:
            for o in self.book.working_orders(key):
                self.book.cancel(o, sig.at, f"strategy {purpose}")
        order, created = self.book.propose(
            key=key,
            side=Side.BUY if buy else Side.SELL,
            order_type=OrderType.MARKET,
            quantity=decision.quantity,
            at=sig.at,
            idempotency_key=idem,
            strategy=sig.strategy,
            purpose=purpose,
            product=self.product,
        )
        if sig.type is SignalType.ENTER_LONG:
            pos = self.book.position(key)
            pos.stop, pos.target = sig.stop, sig.target
        if buy:
            self.entries[(key, day)] += 1
        self.risk_log.append(RiskRecord(proposal, state, decision, order.id))
        if created:
            self.book.transition(order, OrderState.RISK_CHECKED, sig.at, **_summary(decision))
            self.book.submit(order, sig.at)
        return SignalRecord(sig, sig.decision, decision, order.id, diag, advice)

    def _snap_to_tick(self, sig: Signal) -> Signal:
        """Align strategy prices to the instrument's tick, conservatively.

        Stop rounds up (towards the price: never more risk than intended); target rounds
        down (never asks for more than the strategy computed). Unknown tick: unchanged, and
        R5/R6 will fail closed anyway.
        """
        facts = self.env.instruments.get(sig.key)
        if facts is None or facts.tick_size <= 0:
            return sig
        tick = facts.tick_size

        def snap(p: Decimal | None, rounding: str) -> Decimal | None:
            if p is None:
                return None
            return (p / tick).quantize(Decimal(1), rounding=rounding) * tick

        return replace(
            sig, stop=snap(sig.stop, ROUND_CEILING), target=snap(sig.target, ROUND_FLOOR)
        )

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
            idem = f"{group}:{purpose}"
            proposal = OrderProposal(
                key=key,
                horizon=self.horizon,
                side=Side.SELL,
                intent=Intent.REDUCE,
                at=at,
                ref_price=self.marks.get(key, pos.average_price),
                strategy=self.strategy.name,
                idempotency_key=idem,
                stop=stop,
                quantity=pos.quantity,
                limit_price=limit,
                purpose=purpose,
            )
            state, decision = self.assess(proposal, None)
            if not decision.accepted:
                self.risk_log.append(RiskRecord(proposal, state, decision, None))
                continue
            order, created = self.book.propose(
                key=key,
                side=Side.SELL,
                order_type=otype,
                quantity=decision.quantity,
                at=at,
                idempotency_key=idem,
                limit_price=limit,
                stop_price=stop,
                strategy=self.strategy.name,
                purpose=purpose,
                oco_group=group,
                product=self.product,
            )
            self.risk_log.append(RiskRecord(proposal, state, decision, order.id))
            if created:
                self.book.transition(order, OrderState.RISK_CHECKED, at, **_summary(decision))
                self.book.submit(order, at)


@dataclass
class IntradayRunner(BaseRunner):
    """5-minute bars; positions are flat by the session close."""

    def on_bar(
        self, bar: Bar, *, quote_status: str = "FRESH"
    ) -> tuple[list[Fill], list[SignalRecord]]:
        key, day = bar.key, self._day(bar)
        dur = bar.interval.duration
        if dur is None:
            raise ValueError("intraday runner needs fixed-duration bars")
        end = bar.start + dur
        self.start_day(day)
        fills = self._fill_then_protect(bar)  # 1.
        hist = self.history[key]
        hist.append(bar)
        del hist[:-MAX_HISTORY]
        self.marks[key] = bar.close
        self.day_volume[(key, day)] += bar.volume

        pos = self.book.position(key)
        view = self._position_view(key, end)
        today = tuple(b for b in hist if self._day(b) == day)
        ctx = StrategyContext(key, end, today, view, self.entries[(key, day)])
        evaluation = self.strategy.evaluate(ctx)
        self.last_diagnostics[key] = evaluation.diagnostics  # 2.

        records = [
            self._handle(sig, evaluation.diagnostics, quote_status, day)
            for sig in evaluation.signals
        ]
        self.signal_log.extend(records)
        self._sync_protection(key, end)  # 4.

        if end >= self.calendar.session_close(day):  # 5.
            if pos.quantity > 0:
                fill = self.book.force_close(key, bar.close, end, "session square-off")
                if fill is not None:
                    fills.append(fill)
            self.book.expire_working(end, key)
        return fills, records

    def _adv(self, key: InstrumentKey, day: date) -> tuple[Decimal | None, str]:
        if key in self.env.adv:
            return self.env.adv[key]
        prior = sorted(
            (d, v) for (k, d), v in self.day_volume.items() if k == key and d < day and v > 0
        )[-ADV_LOOKBACK_SESSIONS:]
        if prior:
            avg = Decimal(sum(v for _, v in prior)) / len(prior)
            return avg, f"mean of {len(prior)} prior session(s)"
        today = [b for b in self.history.get(key, []) if self._day(b) == day]
        if not today:
            return None, "no volume data"
        dur = today[0].interval.duration
        if dur is None:
            return None, "no volume data"
        session = self.calendar.session_close(day) - self.calendar.session_open(day)
        per_session = Decimal(int(session / dur))
        est = Decimal(sum(b.volume for b in today)) / len(today) * per_session
        return est, "estimated from today's bars"


@dataclass
class SwingRunner(BaseRunner):
    """Daily bars (swing and long-term); positions held across sessions.

    Swing positions keep GTC-style stop/target protection; long-term positions have no
    stop (their thesis and allocation band govern exits).

    A daily bar's decision time is that session's close. Orders decided at a close fill at
    the next session's open. Unfilled entry quantity expires after that next session.
    """

    def bar_end(self, bar: Bar) -> datetime:
        return self.calendar.session_close(self._day(bar))

    def on_bar(
        self, bar: Bar, *, quote_status: str = "FRESH"
    ) -> tuple[list[Fill], list[SignalRecord]]:
        if bar.interval is not Interval.D1:
            raise ValueError("swing runner needs daily bars")
        key, day = bar.key, self._day(bar)
        end = self.bar_end(bar)
        self.start_day(day)
        fills = self._fill_then_protect(bar)
        for o in self.book.working_orders(key):
            if o.purpose in ("entry", "add") and o.created_at < bar.start:
                o.reason = "entry not filled by the next session"
                self.book.transition(o, OrderState.EXPIRED, end, reason=o.reason)
        hist = self.history[key]
        hist.append(bar)
        del hist[:-MAX_HISTORY]
        self.marks[key] = bar.close
        self.day_volume[(key, day)] += bar.volume

        events = tuple(
            EventView(e.kind.value, e.event_date, e.known_at)
            for e in self.env.events.get(key, ())
            if e.known_at <= end and e.event_date >= day
        )
        ctx = StrategyContext(
            key,
            end,
            tuple(hist),
            self._position_view(key, end),
            self.entries[(key, day)],
            events=events,
            bar_end=self.bar_end,
            fundamentals=tuple(known_as_of(self.env.fundamentals.get(key, ()), end)),
            capital=self.env.ledger_allocation,
        )
        evaluation = self.strategy.evaluate(ctx)
        self.last_diagnostics[key] = evaluation.diagnostics
        records = [
            self._handle(sig, evaluation.diagnostics, quote_status, day)
            for sig in evaluation.signals
        ]
        self.signal_log.extend(records)
        self._sync_protection(key, end)
        return fills, records

    def _adv(self, key: InstrumentKey, day: date) -> tuple[Decimal | None, str]:
        if key in self.env.adv:
            return self.env.adv[key]
        vols = [b.volume for b in self.history.get(key, []) if self._day(b) <= day][
            -ADV_LOOKBACK_SESSIONS:
        ]
        vols = [v for v in vols if v > 0]
        if not vols:
            return None, "no volume data"
        return Decimal(sum(vols)) / len(vols), f"mean of {len(vols)} daily bar(s)"


def _summary(d: RiskDecision) -> dict[str, Any]:
    return {
        "outcome": d.outcome.value,
        "quantity": d.quantity,
        "policy_version": d.policy_version,
        "failed": [c.code for c in d.failed],
        "reduced_by": [c.code for c in d.checks if c.status.value == "REDUCE"],
    }


DailyRunner = SwingRunner  # used for both SWING and LONG_TERM ledgers
