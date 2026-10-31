"""Persisted PAPER trading: accounts, cycles, and research-only candidate cycles.

A *cycle* fetches today's completed intraday bars, replays any bars not yet processed through
the shared IntradayRunner (same logic as backtests) and persists orders, fills, events,
positions and signals. New entries are only allowed on the latest bar and only when the live
quote is FRESH; catch-up bars may fill or exit but never open positions (fail closed).

PAPER cycles require ``trading_mode=PAPER`` and ``features.paper_trading``. Otherwise only a
research cycle is possible: candidates are recorded, no order is created.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.config import Settings, TradingMode
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, Clock
from rhythm.core.errors import RhythmError
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey
from rhythm.db.models import (
    FillRow,
    OrderEventRow,
    OrderRow,
    PaperAccount,
    PaperPosition,
    StrategySignalRow,
)
from rhythm.execution.book import ACCOUNT_KIND, Order, PaperBook, Position
from rhythm.execution.costs import CostModel, Product, Side
from rhythm.execution.fills import FillModel, OrderType
from rhythm.execution.runner import IntradayRunner, SignalRecord
from rhythm.instruments.registry import get_active
from rhythm.marketdata.freshness import FreshnessPolicy, FreshnessStatus, evaluate_quote
from rhythm.marketdata.provider import MarketDataProvider, MarketDataUnavailableError
from rhythm.marketdata.types import Interval
from rhythm.orders.state import TERMINAL, OrderState
from rhythm.strategies.base import Strategy, params_hash


class PaperTradingDisabledError(RhythmError):
    """PAPER orders need trading_mode=PAPER and features.paper_trading=true."""


class AccountNotFoundError(RhythmError):
    pass


def paper_enabled(settings: Settings) -> bool:
    return settings.trading_mode is TradingMode.PAPER and settings.features.paper_trading


def _uuid_ids(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


@dataclass
class CycleResult:
    mode: str  # PAPER | RESEARCH
    account_id: int | None
    as_of: datetime
    processed_bars: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    signals: list[dict[str, Any]] = field(default_factory=list)
    new_orders: int = 0
    new_fills: int = 0
    cash: str | None = None
    equity: str | None = None
    data_is_mock: bool = False


# ---------------------------------------------------------------------------- accounts
def create_account(
    session: Session, audit: AuditLog, *, name: str, starting_cash: Decimal, actor_id: str
) -> PaperAccount:
    if starting_cash <= 0:
        raise ValueError("starting cash must be positive")
    acct = PaperAccount(name=name, starting_cash=starting_cash, cash=starting_cash, state={})
    session.add(acct)
    session.flush()
    audit.append(
        session,
        event_type="paper.account_created",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="paper_account",
        subject_id=str(acct.id),
        payload={"name": name, "starting_cash": str(starting_cash), "account_kind": ACCOUNT_KIND},
    )
    return acct


def get_account(session: Session, account_id: int, *, lock: bool = False) -> PaperAccount:
    stmt = select(PaperAccount).where(PaperAccount.id == account_id)
    if lock:
        stmt = stmt.with_for_update()  # one cycle per account at a time
    acct = session.execute(stmt).scalar_one_or_none()
    if acct is None:
        raise AccountNotFoundError(f"paper account {account_id} not found")
    return acct


# ---------------------------------------------------------------------------- book <-> db
def _key(exchange: str, symbol: str) -> InstrumentKey:
    return InstrumentKey(Exchange(exchange), symbol)


def load_book(
    session: Session,
    acct: PaperAccount,
    *,
    since: datetime,
    cost_model: CostModel | None = None,
    fill_model: FillModel | None = None,
) -> PaperBook:
    book = PaperBook(
        cash=acct.cash,
        cost_model=cost_model or CostModel(),
        fill_model=fill_model or FillModel(),
        id_factory=_uuid_ids,
    )
    for p in session.execute(
        select(PaperPosition).where(PaperPosition.account_id == acct.id)
    ).scalars():
        book.positions[_key(p.exchange, p.symbol)] = Position(
            key=_key(p.exchange, p.symbol),
            quantity=p.quantity,
            average_price=p.average_price,
            realized_pnl=p.realized_pnl,
            charges_paid=p.charges_paid,
            opened_at=p.opened_at,
            stop=p.stop,
            target=p.target,
        )
    open_states = [
        s.value for s in OrderState if s not in TERMINAL and s is not OrderState.RECONCILED
    ]
    rows = session.execute(
        select(OrderRow).where(
            OrderRow.account_id == acct.id,
            or_(OrderRow.state.in_(open_states), OrderRow.created_at >= since),
        )
    ).scalars()
    for r in rows:
        o = Order(
            id=r.id,
            key=_key(r.exchange, r.symbol),
            side=Side(r.side),
            order_type=OrderType(r.order_type),
            quantity=r.quantity,
            created_at=r.created_at,
            idempotency_key=r.idempotency_key,
            product=Product(r.product),
            limit_price=r.limit_price,
            stop_price=r.stop_price,
            state=OrderState(r.state),
            filled_quantity=r.filled_quantity,
            average_fill_price=r.average_fill_price,
            strategy=r.strategy,
            purpose=r.purpose,
            oco_group=r.oco_group,
            reason=r.reason,
            updated_at=r.updated_at,
        )
        book.orders[o.id] = o
        book._by_idem[o.idempotency_key] = o.id
    return book


def save_book(session: Session, acct: PaperAccount, book: PaperBook, *, data_is_mock: bool) -> None:
    for o in book.orders.values():
        session.merge(
            OrderRow(
                id=o.id,
                account_kind=ACCOUNT_KIND,
                account_id=acct.id,
                idempotency_key=o.idempotency_key,
                exchange=o.key.exchange.value,
                symbol=o.key.symbol,
                side=o.side.value,
                order_type=o.order_type.value,
                product=o.product.value,
                quantity=o.quantity,
                filled_quantity=o.filled_quantity,
                average_fill_price=o.average_fill_price,
                limit_price=o.limit_price,
                stop_price=o.stop_price,
                state=o.state.value,
                purpose=o.purpose,
                oco_group=o.oco_group,
                strategy=o.strategy,
                reason=o.reason,
                data_is_mock=data_is_mock,
                created_at=o.created_at,
                updated_at=o.updated_at or o.created_at,
            )
        )
    session.flush()
    for f in book.fills:
        session.add(
            FillRow(
                id=f.id,
                account_kind=ACCOUNT_KIND,
                account_id=acct.id,
                order_id=f.order_id,
                exchange=f.key.exchange.value,
                symbol=f.key.symbol,
                side=f.side.value,
                quantity=f.quantity,
                price=f.price,
                charges_total=f.charges.total,
                charges=f.charges.as_dict(),
                at=f.at,
            )
        )
    for e in book.events:
        session.add(
            OrderEventRow(
                order_id=e.order_id,
                from_state=None if e.from_state is None else e.from_state.value,
                to_state=e.to_state.value,
                at=e.at,
                detail={
                    k: str(v) if not isinstance(v, (dict, list, int, bool)) else v
                    for k, v in e.detail.items()
                },
            )
        )
    for k, p in book.positions.items():
        session.merge(
            PaperPosition(
                account_id=acct.id,
                exchange=k.exchange.value,
                symbol=k.symbol,
                quantity=p.quantity,
                average_price=p.average_price,
                realized_pnl=p.realized_pnl,
                charges_paid=p.charges_paid,
                opened_at=p.opened_at,
                stop=p.stop,
                target=p.target,
            )
        )
    acct.cash = book.cash
    session.flush()


# ---------------------------------------------------------------------------- cycle
def _signal_row(
    rec: SignalRecord,
    *,
    context: str,
    account_id: int | None,
    strategy: Strategy,
    persist_orders: bool,
    data_is_mock: bool,
) -> StrategySignalRow:
    s = rec.signal
    risk = rec.risk.as_dict()
    if not persist_orders:
        risk["note"] = "research only: no order created"
    return StrategySignalRow(
        context=context,
        account_id=account_id,
        strategy=s.strategy,
        version=s.version,
        params_hash=params_hash(strategy.params),
        exchange=s.key.exchange.value,
        symbol=s.key.symbol,
        horizon=s.horizon.value,
        signal_type=s.type.value,
        decision_state=rec.decision.value,
        decided_at=s.at,
        ref_price=s.ref_price,
        stop=s.stop,
        target=s.target,
        reasons=list(s.reasons),
        risk=risk,
        diagnostics={k: v for k, v in rec.diagnostics.items() if k != "checks"}
        | {"checks": {k: bool(v) for k, v in rec.diagnostics.get("checks", {}).items()}},
        order_id=rec.order_id if persist_orders else None,
        data_is_mock=data_is_mock,
    )


def run_cycle(
    session: Session,
    *,
    keys: Sequence[InstrumentKey],
    provider: MarketDataProvider,
    strategy: Strategy,
    settings: Settings,
    clock: Clock,
    calendar: SessionCalendar,
    audit: AuditLog,
    actor_id: str,
    account_id: int | None = None,
    policy: FreshnessPolicy | None = None,
) -> CycleResult:
    persist = account_id is not None
    if persist and not paper_enabled(settings):
        raise PaperTradingDisabledError(
            "PAPER cycles need RHYTHM_TRADING_MODE=PAPER and RHYTHM_FEATURES__PAPER_TRADING=true"
        )
    now = clock.now()
    day = now.astimezone(EXCHANGE_TZ).date()
    mock = provider.origin is DataOrigin.MOCK
    result = CycleResult("PAPER" if persist else "RESEARCH", account_id, now, data_is_mock=mock)
    if not calendar.is_trading_day(day):
        result.skipped = {str(k): "not a trading day" for k in keys}
        return result

    session_open = calendar.session_open(day)
    acct = get_account(session, account_id, lock=True) if account_id is not None else None
    book = (
        load_book(session, acct, since=session_open)
        if acct is not None
        else PaperBook(cash=Decimal(1_000_000), id_factory=_uuid_ids)
    )
    state: dict[str, Any] = dict(acct.state) if acct is not None else {}
    if state.get("day") != day.isoformat():
        state = {"day": day.isoformat(), "last_bar": {}, "entries": {}}
    runner = IntradayRunner(strategy=strategy, book=book, calendar=calendar)
    if "day_start_equity" in state:
        runner.day_start_equity[day] = Decimal(state["day_start_equity"])
    else:
        runner.day_start_equity[day] = book.equity({})
        state["day_start_equity"] = str(runner.day_start_equity[day])
    loaded_orders = set(book.orders)

    for key in keys:
        name = str(key)
        inst = get_active(session, key)
        if inst is None:
            result.skipped[name] = "not an active instrument in the registry"
            continue
        book.ticks[key] = inst.tick_size
        runner.lot_sizes[key] = inst.lot_size
        runner.entries[(key, day)] = int(state["entries"].get(name, 0))
        try:
            bars = list(provider.bars(key, Interval.M5, session_open, now))
            quote = provider.latest_quote(key)
        except MarketDataUnavailableError as exc:
            result.skipped[name] = f"market data unavailable: {exc}"
            continue
        fresh = evaluate_quote(quote, now, calendar, policy).status is FreshnessStatus.FRESH
        last = state["last_bar"].get(name)
        older = [b for b in bars if last is not None and b.start.isoformat() <= last]
        new = [b for b in bars if last is None or b.start.isoformat() > last]
        runner.history[key] = older
        if older:
            runner.marks[key] = older[-1].close
        for i, bar in enumerate(new):
            _, recs = runner.on_bar(bar, data_fresh=fresh and i == len(new) - 1)
            for rec in recs:
                row = _signal_row(
                    rec,
                    context="PAPER" if persist else "RESEARCH",
                    account_id=account_id,
                    strategy=strategy,
                    persist_orders=persist,
                    data_is_mock=mock,
                )
                session.add(row)
                result.signals.append(
                    {
                        "instrument": name,
                        "type": rec.signal.type.value,
                        "decision": rec.decision.value,
                        "at": rec.signal.at.isoformat(),
                        "risk": rec.risk.outcome.value,
                        "order_id": rec.order_id if persist else None,
                    }
                )
        result.processed_bars[name] = len(new)
        if bars:
            state["last_bar"][name] = bars[-1].start.isoformat()
        state["entries"][name] = runner.entries[(key, day)]
        if not fresh and new:
            result.skipped.setdefault(name, "quote not FRESH: entries blocked")

    if acct is not None:
        result.new_orders = len(set(book.orders) - loaded_orders)
        result.new_fills = len(book.fills)
        save_book(session, acct, book, data_is_mock=mock)
        acct.state = state
        result.cash = str(book.cash)
        result.equity = str(book.equity(runner.marks).quantize(Decimal("0.01")))
        audit.append(
            session,
            event_type="paper.cycle",
            actor_type=ActorType.USER,
            actor_id=actor_id,
            subject_type="paper_account",
            subject_id=str(acct.id),
            payload={
                "account_kind": ACCOUNT_KIND,
                "strategy": f"{strategy.name}@{strategy.version}",
                "new_orders": result.new_orders,
                "new_fills": result.new_fills,
                "signals": len(result.signals),
                "data_is_mock": mock,
            },
        )
    session.flush()
    return result
