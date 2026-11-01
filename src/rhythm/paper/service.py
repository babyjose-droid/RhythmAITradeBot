"""Persisted PAPER trading: accounts, cycles, and research-only candidate cycles.

A *cycle* fetches today's completed intraday bars, replays any bars not yet processed through
the shared IntradayRunner (same logic as backtests) and persists orders, fills, events,
positions and signals. New entries are only allowed on the latest bar and only when the live
quote is FRESH; catch-up bars may fill or exit but never open positions (fail closed).

PAPER cycles require ``trading_mode=PAPER`` and ``features.paper_trading``. Otherwise only a
research cycle is possible: candidates are recorded, no order is created.

Phase 4: each cycle reconciles the account against its fill journal before and after trading,
resolves UNKNOWN orders against the venue, applies the kill switch / strategy pauses and the
active risk policy, persists every risk decision, and auto-pauses a strategy that hits its
daily loss stop. A reconciliation break activates the kill switch.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.config import Settings, TradingMode
from rhythm.controls import service as controls
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, Clock
from rhythm.core.errors import RhythmError
from rhythm.core.market import DataOrigin, Exchange, Horizon, InstrumentKey
from rhythm.db.models import (
    FillRow,
    OrderEventRow,
    OrderRow,
    PaperAccount,
    PaperPosition,
    StrategySignalRow,
)
from rhythm.events import store as event_store
from rhythm.execution.book import ACCOUNT_KIND, Order, PaperBook, Position
from rhythm.execution.costs import CostModel, Product, Side
from rhythm.execution.fills import FillModel, OrderType
from rhythm.execution.gateway import OrderVenue, PaperVenue
from rhythm.execution.runner import (
    MAX_HISTORY,
    BaseRunner,
    IntradayRunner,
    RiskEnvironment,
    SignalRecord,
    SwingRunner,
)
from rhythm.instruments.registry import get_active
from rhythm.marketdata.bars import get_bars
from rhythm.marketdata.freshness import (
    FreshnessPolicy,
    FreshnessStatus,
    evaluate_daily,
    evaluate_quote,
)
from rhythm.marketdata.provider import MarketDataProvider, MarketDataUnavailableError
from rhythm.marketdata.types import Interval
from rhythm.orders.state import TERMINAL, OrderState
from rhythm.paper import theses
from rhythm.portfolio import service as portfolio
from rhythm.risk.engine import InstrumentFacts, Intent, OrderProposal
from rhythm.risk.policy import RiskPolicy
from rhythm.strategies.base import Strategy, params_hash
from rhythm.strategies.intraday_orb import IntradayOrbVwap

DISABLED_MSG = "PAPER cycles need RHYTHM_TRADING_MODE=PAPER and RHYTHM_FEATURES__PAPER_TRADING=true"


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
    controls: dict[str, Any] = field(default_factory=dict)
    unknown_resolution: dict[str, str] = field(default_factory=dict)
    risk_decisions: int = 0
    auto_paused: list[str] = field(default_factory=list)
    horizon: str = "INTRADAY"
    theses: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------- accounts
def create_account(
    session: Session,
    audit: AuditLog,
    *,
    name: str,
    starting_cash: Decimal,
    actor_id: str,
    policy: RiskPolicy | None = None,
) -> PaperAccount:
    if starting_cash <= 0:
        raise ValueError("starting cash must be positive")
    acct = PaperAccount(name=name, starting_cash=starting_cash, cash=starting_cash, state={})
    session.add(acct)
    session.flush()
    alloc = portfolio.create_ledgers(session, acct, policy or RiskPolicy())
    audit.append(
        session,
        event_type="paper.account_created",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="paper_account",
        subject_id=str(acct.id),
        payload={
            "name": name,
            "starting_cash": str(starting_cash),
            "account_kind": ACCOUNT_KIND,
            "ledgers": {lg.horizon: str(lg.allocated_capital) for lg in alloc},
        },
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
    horizon: Horizon = Horizon.INTRADAY,
    venue: OrderVenue | None = None,
) -> PaperBook:
    """Load one ledger's book. Cash is the account's shared cash."""
    book = PaperBook(
        cash=acct.cash,
        cost_model=cost_model or CostModel(),
        fill_model=fill_model or FillModel(),
        id_factory=_uuid_ids,
        venue=venue or PaperVenue(),
        horizon=horizon,
    )
    for p in session.execute(
        select(PaperPosition).where(
            PaperPosition.account_id == acct.id, PaperPosition.horizon == horizon.value
        )
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
            OrderRow.horizon == horizon.value,
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
            venue_order_id=r.venue_order_id,
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
                horizon=book.horizon.value,
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
                venue_order_id=o.venue_order_id,
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
                horizon=book.horizon.value,
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
                horizon=book.horizon.value,
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


# ---------------------------------------------------------------------------- risk inputs
def instrument_facts(session: Session, key: InstrumentKey) -> InstrumentFacts | None:
    inst = get_active(session, key)
    if inst is None:
        return None
    return InstrumentFacts(
        active=True,
        instrument_type=inst.instrument_type,
        lot_size=inst.lot_size,
        tick_size=inst.tick_size,
    )


def stored_adv(
    session: Session, key: InstrumentKey, now: datetime, sessions: int = 20
) -> tuple[Decimal, str] | None:
    """Average daily volume from stored daily bars (any origin), if there are any."""
    rows = get_bars(session, key, Interval.D1, now - timedelta(days=sessions * 2), now)
    vols = [r.volume for r in rows[-sessions:] if r.volume > 0]
    if not vols:
        return None
    origin = rows[-1].source if rows else "?"
    return Decimal(sum(vols)) / len(vols), f"stored daily bars ({len(vols)}, {origin})"


def _other_ledger_exposure(
    session: Session, account_id: int, horizon: Horizon
) -> dict[InstrumentKey, Decimal]:
    out: dict[InstrumentKey, Decimal] = {}
    for p in portfolio.positions(session, account_id):
        if p.horizon != horizon.value and p.quantity > 0:
            k = _key(p.exchange, p.symbol)
            out[k] = out.get(k, Decimal(0)) + p.average_price * p.quantity
    return out


def _reconcile(
    session: Session,
    audit: AuditLog,
    acct: PaperAccount,
    *,
    at: datetime,
    trigger: str,
    actor_id: str,
) -> bool:
    result = portfolio.reconcile_account(session, acct)
    portfolio.record_reconciliation(
        session, audit, acct=acct, result=result, at=at, trigger=trigger, actor_id=actor_id
    )
    if not result.ok:
        controls.set_kill_switch(
            session,
            audit,
            active=True,
            actor_type=ActorType.SYSTEM,
            actor_id="reconciliation",
            reason=f"reconciliation break on PAPER account {acct.id} ({trigger})",
            at=at,
            detail={"account_id": acct.id, "diffs": [d.as_dict() for d in result.diffs[:10]]},
        )
    return result.ok


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


def _ledger_state(acct: PaperAccount | None, horizon: Horizon) -> dict[str, Any]:
    """Per-ledger cycle state. Phase 3/4 stored the intraday state at the top level."""
    raw: dict[str, Any] = dict(acct.state) if acct is not None else {}
    if "day" in raw and Horizon.INTRADAY.value not in raw:
        raw = {Horizon.INTRADAY.value: raw}
    return dict(raw.get(horizon.value, {}))


def _store_ledger_state(acct: PaperAccount, horizon: Horizon, st: dict[str, Any]) -> None:
    raw: dict[str, Any] = dict(acct.state)
    if "day" in raw and Horizon.INTRADAY.value not in raw:
        raw = {Horizon.INTRADAY.value: raw}
    raw[horizon.value] = st
    acct.state = raw


def _prime_pnl(runner: BaseRunner, st: dict[str, Any], day: date) -> None:
    """Restore today's ledger P&L baseline saved by earlier cycles (or start it now)."""
    if st.get("pnl_day") == day.isoformat():
        runner.carried_flow[day] = Decimal(st.get("day_flow", "0"))
        runner.day_flow_base[day] = runner.book.flow
        if "day_start_value" in st:
            runner.day_start_value[day] = Decimal(st["day_start_value"])
        if "day_start_equity" in st:
            runner.day_start_equity[day] = Decimal(st["day_start_equity"])
    runner.start_day(day)
    st["pnl_day"] = day.isoformat()
    st["day_start_value"] = str(runner.day_start_value[day])
    st["day_start_equity"] = str(runner.day_start_equity[day])


@dataclass
class _Ledger:
    acct: PaperAccount | None
    book: PaperBook
    env: RiskEnvironment
    ctl: controls.ControlState
    risk_policy: RiskPolicy
    persist: bool


def _open_ledger(
    session: Session,
    *,
    horizon: Horizon,
    settings: Settings,
    now: datetime,
    since: datetime,
    audit: AuditLog,
    actor_id: str,
    account_id: int | None,
    venue: OrderVenue | None,
) -> _Ledger:
    persist = account_id is not None
    if persist and not paper_enabled(settings):
        raise PaperTradingDisabledError(DISABLED_MSG)
    acct = get_account(session, account_id, lock=True) if account_id is not None else None
    risk_policy = controls.active_policy(session, now)
    env = RiskEnvironment(
        mode="PAPER" if persist else "SIMULATION",
        policy=risk_policy,
        book_is_truth=not persist,
        evaluated_at=now,
    )
    if acct is not None:
        recon_ok = _reconcile(session, audit, acct, at=now, trigger="pre-cycle", actor_id=actor_id)
        env.reconciled_at, env.reconciliation_ok = now, recon_ok
        ledger = portfolio.ledgers(session, acct.id).get(horizon.value)
        env.ledger_allocation = None if ledger is None else ledger.allocated_capital
        env.other_ledger_exposure = _other_ledger_exposure(session, acct.id, horizon)
        book = load_book(session, acct, since=since, venue=venue, horizon=horizon)
    else:
        book = PaperBook(cash=Decimal(1_000_000), id_factory=_uuid_ids, horizon=horizon)
        env.ledger_allocation = book.cash * risk_policy.ledger_allocation_pct[horizon.value]
    ctl = controls.control_state(session)
    env.kill_switch_active = ctl.kill_switch_active
    env.paused_strategies = ctl.paused_strategies
    return _Ledger(acct, book, env, ctl, risk_policy, persist)


def _record_signal_rows(
    session: Session,
    result: CycleResult,
    recs: list[SignalRecord],
    *,
    name: str,
    lg: _Ledger,
    strategy: Strategy,
    mock: bool,
) -> None:
    for rec in recs:
        session.add(
            _signal_row(
                rec,
                context="PAPER" if lg.persist else "RESEARCH",
                account_id=lg.acct.id if lg.acct is not None else None,
                strategy=strategy,
                persist_orders=lg.persist,
                data_is_mock=mock,
            )
        )
        result.signals.append(
            {
                "instrument": name,
                "type": rec.signal.type.value,
                "decision": rec.decision.value,
                "at": rec.signal.at.isoformat(),
                "risk": rec.risk.outcome.value,
                "failed_checks": [c.code for c in rec.risk.failed],
                "reasons": list(rec.signal.reasons),
                "order_id": rec.order_id if lg.persist else None,
            }
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
    venue: OrderVenue | None = None,
) -> CycleResult:
    """Run one cycle for the strategy's horizon (INTRADAY 5-minute or SWING daily)."""
    if strategy.horizon is Horizon.SWING:
        return _run_swing(
            session,
            keys=keys,
            provider=provider,
            strategy=strategy,
            settings=settings,
            clock=clock,
            calendar=calendar,
            audit=audit,
            actor_id=actor_id,
            account_id=account_id,
            venue=venue,
        )
    if strategy.horizon is not Horizon.INTRADAY:
        raise ValueError(f"no PAPER workflow for {strategy.horizon.value} yet")
    now = clock.now()
    day = now.astimezone(EXCHANGE_TZ).date()
    mock = provider.origin is DataOrigin.MOCK
    persist = account_id is not None
    result = CycleResult("PAPER" if persist else "RESEARCH", account_id, now, data_is_mock=mock)
    result.horizon = Horizon.INTRADAY.value
    if not calendar.is_trading_day(day):
        if persist and not paper_enabled(settings):
            raise PaperTradingDisabledError(DISABLED_MSG)
        result.skipped = {str(k): "not a trading day" for k in keys}
        return result

    session_open = calendar.session_open(day)
    lg = _open_ledger(
        session,
        horizon=Horizon.INTRADAY,
        settings=settings,
        now=now,
        since=session_open,
        audit=audit,
        actor_id=actor_id,
        account_id=account_id,
        venue=venue,
    )
    book, env = lg.book, lg.env
    result.controls = lg.ctl.as_dict()
    result.unknown_resolution = book.resolve_unknown(now)

    state = _ledger_state(lg.acct, Horizon.INTRADAY)
    if state.get("day") != day.isoformat():
        state = {"day": day.isoformat(), "last_bar": {}, "entries": {}}
    runner = IntradayRunner(strategy=strategy, book=book, calendar=calendar, env=env)
    _prime_pnl(runner, state, day)
    loaded_orders = set(book.orders)
    loaded_fills = len(book.fills)

    for key in keys:
        name = str(key)
        facts = instrument_facts(session, key)
        if facts is None:
            result.skipped[name] = "not an active instrument in the registry"
            continue
        env.instruments[key] = facts
        book.ticks[key] = facts.tick_size
        adv = stored_adv(session, key, now)
        if adv is not None:
            env.adv[key] = adv
        runner.entries[(key, day)] = int(state["entries"].get(name, 0))
        try:
            bars = list(provider.bars(key, Interval.M5, session_open, now))
            quote = provider.latest_quote(key)
        except MarketDataUnavailableError as exc:
            result.skipped[name] = f"market data unavailable: {exc}"
            continue
        status = evaluate_quote(quote, now, calendar, policy).status
        if (
            quote is not None
            and quote.lower_circuit is not None
            and quote.upper_circuit is not None
        ):
            env.instruments[key] = replace(
                facts, lower_circuit=quote.lower_circuit, upper_circuit=quote.upper_circuit
            )
        last = state["last_bar"].get(name)
        older = [b for b in bars if last is not None and b.start.isoformat() <= last]
        new = [b for b in bars if last is None or b.start.isoformat() > last]
        runner.history[key] = older
        for b in older:
            runner.day_volume[(key, day)] += b.volume
        if older:
            runner.marks[key] = older[-1].close
        for i, bar in enumerate(new):
            # Only the newest bar can open positions; replayed bars are not current data.
            qstatus = status.value if i == len(new) - 1 else "CATCH_UP"
            _, recs = runner.on_bar(bar, quote_status=qstatus)
            _record_signal_rows(
                session, result, recs, name=name, lg=lg, strategy=strategy, mock=mock
            )
        result.processed_bars[name] = len(new)
        if bars:
            state["last_bar"][name] = bars[-1].start.isoformat()
        state["entries"][name] = runner.entries[(key, day)]
        if status is not FreshnessStatus.FRESH and new:
            result.skipped.setdefault(name, f"quote {status.value}: entries blocked")

    state["day_flow"] = str(runner.day_flow(day))
    _finish(
        session,
        lg,
        runner,
        state,
        result,
        loaded_orders=loaded_orders,
        loaded_fills=loaded_fills,
        strategy=strategy,
        audit=audit,
        actor_id=actor_id,
        now=now,
        mock=mock,
    )
    return result


SWING_LOOKBACK_DAYS = 400


def _run_swing(
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
    account_id: int | None,
    venue: OrderVenue | None,
) -> CycleResult:
    """Daily cycle for multi-day positions. Run after the close (or before the next open).

    The first cycle for an instrument uses the history only as warm-up and evaluates the
    latest completed session; later cycles process every session not yet seen. Only the
    latest session, when its daily bar is FRESH, may open positions.
    """
    now = clock.now()
    mock = provider.origin is DataOrigin.MOCK
    persist = account_id is not None
    result = CycleResult("PAPER" if persist else "RESEARCH", account_id, now, data_is_mock=mock)
    result.horizon = Horizon.SWING.value
    lg = _open_ledger(
        session,
        horizon=Horizon.SWING,
        settings=settings,
        now=now,
        since=now - timedelta(days=7),
        audit=audit,
        actor_id=actor_id,
        account_id=account_id,
        venue=venue,
    )
    book, env = lg.book, lg.env
    result.controls = lg.ctl.as_dict()
    result.unknown_resolution = book.resolve_unknown(now)
    state = _ledger_state(lg.acct, Horizon.SWING)
    state.setdefault("last_bar", {})
    runner = SwingRunner(strategy=strategy, book=book, calendar=calendar, env=env)
    loaded_orders = set(book.orders)
    loaded_fills = len(book.fills)
    today = now.astimezone(EXCHANGE_TZ).date()
    latest_day: date | None = None

    for key in keys:
        name = str(key)
        facts = instrument_facts(session, key)
        if facts is None:
            result.skipped[name] = "not an active instrument in the registry"
            continue
        env.instruments[key] = facts
        book.ticks[key] = facts.tick_size
        try:
            bars = list(
                provider.bars(key, Interval.D1, now - timedelta(days=SWING_LOOKBACK_DAYS), now)
            )
        except MarketDataUnavailableError as exc:
            result.skipped[name] = f"market data unavailable: {exc}"
            continue
        if not bars:
            result.skipped[name] = "no daily bars"
            continue
        env.events[key] = tuple(
            event_store.known_events(
                session,
                key,
                now,
                today - timedelta(days=SWING_LOOKBACK_DAYS),
                today + timedelta(days=120),
            )
        )
        synced = event_store.last_sync(session, key, now)
        if synced is not None:
            env.event_synced_at[key] = synced
        status = evaluate_daily(bars[-1].start, now, calendar).status
        last = state["last_bar"].get(name)
        if last is None:
            older, new = bars[:-1], bars[-1:]
        else:
            older = [b for b in bars if b.start.isoformat() <= last]
            new = [b for b in bars if b.start.isoformat() > last]
        runner.history[key] = older[-MAX_HISTORY:]
        if older:
            runner.marks[key] = older[-1].close
        for i, bar in enumerate(new):
            qstatus = status.value if i == len(new) - 1 else "CATCH_UP"
            bday = bar.start.astimezone(EXCHANGE_TZ).date()
            if latest_day is None or bday > latest_day:
                latest_day = bday
            if bday == latest_day:
                _prime_pnl(runner, state, bday)
            else:
                runner.start_day(bday)
            _, recs = runner.on_bar(bar, quote_status=qstatus)
            _record_signal_rows(
                session, result, recs, name=name, lg=lg, strategy=strategy, mock=mock
            )
        result.processed_bars[name] = len(new)
        state["last_bar"][name] = bars[-1].start.isoformat()
        if status is not FreshnessStatus.FRESH and new:
            result.skipped.setdefault(name, f"daily bar {status.value}: entries blocked")
        if key not in env.event_synced_at:
            result.skipped.setdefault(name, "event calendar never synced: entries blocked (R18)")

    if latest_day is not None:
        state["day_flow"] = str(runner.day_flow(latest_day))
    _finish(
        session,
        lg,
        runner,
        state,
        result,
        loaded_orders=loaded_orders,
        loaded_fills=loaded_fills,
        strategy=strategy,
        audit=audit,
        actor_id=actor_id,
        now=now,
        mock=mock,
    )
    if lg.acct is not None:
        # 1) advance status from fills (an entry may have filled), 2) record today's new
        # entries and exit-invalidations, 3) close theses whose position is now flat.
        nxt = calendar.session_close(_next_trading_day(calendar, today))
        common: dict[str, Any] = {
            "account_id": lg.acct.id,
            "horizon": Horizon.SWING,
            "book": book,
            "at": now,
            "next_review_at": nxt,
        }
        before = theses.sync(session, diagnostics={}, **common)
        created, invalidated = theses.record_signals(
            session,
            account_id=lg.acct.id,
            horizon=Horizon.SWING,
            records=runner.signal_log,
            new_order_ids=set(book.orders) - loaded_orders,
            data_is_mock=mock,
            at=now,
        )
        after = theses.sync(session, diagnostics=runner.last_diagnostics, **common)
        result.theses = {
            "created": created,
            "invalidated": invalidated,
            "opened": before["opened"] + after["opened"],
            "closed": before["closed"] + after["closed"],
            "checked": after["checked"],
        }
    return result


def _next_trading_day(calendar: SessionCalendar, d: date) -> date:
    nd = d + timedelta(days=1)
    while not calendar.is_trading_day(nd):
        nd += timedelta(days=1)
    return nd


def _finish(
    session: Session,
    lg: _Ledger,
    runner: BaseRunner,
    state: dict[str, Any],
    result: CycleResult,
    *,
    loaded_orders: set[str],
    loaded_fills: int,
    strategy: Strategy,
    audit: AuditLog,
    actor_id: str,
    now: datetime,
    mock: bool,
) -> None:
    book, acct = lg.book, lg.acct
    if acct is not None:
        result.new_orders = len(set(book.orders) - loaded_orders)
        result.new_fills = len(book.fills) - loaded_fills
        save_book(session, acct, book, data_is_mock=mock)
        _store_ledger_state(acct, book.horizon, state)
    _persist_risk(session, runner, account_id=result.account_id, persist_orders=lg.persist)
    result.risk_decisions = len(runner.risk_log)
    breached = sorted(
        {
            r.proposal.strategy
            for r in runner.risk_log
            if any(c.code == "R8" for c in r.decision.failed)
        }
        - lg.ctl.paused_strategies
    )
    for name in breached:
        controls.set_strategy_paused(
            session,
            audit,
            strategy=name,
            paused=True,
            actor_type=ActorType.SYSTEM,
            actor_id="risk-engine",
            reason="daily loss stop reached (R8)",
            at=now,
            detail={"account_id": result.account_id, "context": result.mode},
        )
        result.auto_paused.append(name)

    if acct is not None:
        session.flush()
        _reconcile(session, audit, acct, at=now, trigger="post-cycle", actor_id=actor_id)
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
                "horizon": book.horizon.value,
                "strategy": f"{strategy.name}@{strategy.version}",
                "policy": lg.risk_policy.version,
                "new_orders": result.new_orders,
                "new_fills": result.new_fills,
                "signals": len(result.signals),
                "unknown_resolution": result.unknown_resolution,
                "auto_paused": result.auto_paused,
                "data_is_mock": mock,
            },
        )
    session.flush()


def _persist_risk(
    session: Session, runner: BaseRunner, *, account_id: int | None, persist_orders: bool
) -> None:
    context = "PAPER" if persist_orders else "SIMULATION"
    for rec in runner.risk_log:
        session.add(
            controls.risk_check_row(
                context=context,
                account_id=account_id,
                proposal=rec.proposal,
                state=rec.state,
                decision=rec.decision,
                order_id=rec.order_id if persist_orders else None,
            )
        )
    session.flush()


# ---------------------------------------------------------------------------- kill switch
@dataclass
class KillSwitchResult:
    changed: bool
    cancelled_orders: list[str] = field(default_factory=list)
    flatten_orders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def activate_kill_switch(
    session: Session,
    audit: AuditLog,
    *,
    settings: Settings,
    actor_type: ActorType,
    actor_id: str,
    reason: str,
    at: datetime,
    flatten_intraday: bool = False,
) -> KillSwitchResult:
    """Block new risk, cancel open risk-increasing PAPER orders, optionally flatten intraday.

    Protective stop/target orders stay in place so open positions remain protected. Flatten
    orders are risk-reducing and still pass the risk engine; they fill on the next cycle.
    """
    out = KillSwitchResult(
        controls.set_kill_switch(
            session,
            audit,
            active=True,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            at=at,
            detail={"flatten_intraday": flatten_intraday},
        )
    )
    accounts = list(session.execute(select(PaperAccount).with_for_update()).scalars())
    for acct in accounts:
        for horizon in (Horizon.INTRADAY, Horizon.SWING):
            book = load_book(session, acct, since=at, horizon=horizon)
            for o in list(book.orders.values()):
                if o.purpose == "entry" and o.state in (
                    OrderState.RISK_CHECKED,
                    OrderState.APPROVED,
                    OrderState.BROKER_ACKNOWLEDGED,
                    OrderState.PARTIALLY_FILLED,
                ):
                    book.cancel(o, at, "kill switch")
                    out.cancelled_orders.append(o.id)
            if flatten_intraday and horizon is Horizon.INTRADAY:
                if not paper_enabled(settings):
                    out.notes.append("flatten skipped: PAPER trading is not enabled")
                else:
                    out.flatten_orders += _flatten(session, acct, book, at)
            save_book(session, acct, book, data_is_mock=_account_is_mock(session, acct))
    session.flush()
    return out


def _account_is_mock(session: Session, acct: PaperAccount) -> bool:
    row = session.execute(
        select(OrderRow.data_is_mock).where(OrderRow.account_id == acct.id).limit(1)
    ).scalar_one_or_none()
    return bool(row) if row is not None else True


def _flatten(session: Session, acct: PaperAccount, book: PaperBook, at: datetime) -> list[str]:
    env = RiskEnvironment(mode="PAPER", kill_switch_active=True, book_is_truth=True)
    runner = IntradayRunner(
        strategy=IntradayOrbVwap(), book=book, calendar=SessionCalendar(), env=env
    )
    made: list[str] = []
    for key, pos in list(book.positions.items()):
        if pos.quantity <= 0 or any(o.purpose == "square_off" for o in book.working_orders(key)):
            continue
        facts = instrument_facts(session, key)
        if facts is not None:
            env.instruments[key] = facts
        p = OrderProposal(
            key=key,
            horizon=book.horizon,
            side=Side.SELL,
            intent=Intent.REDUCE,
            at=at,
            ref_price=pos.average_price,
            strategy="kill-switch",
            idempotency_key=f"kill-flatten:{acct.id}:{key}:{at.isoformat()}",
            quantity=pos.quantity,
            purpose="square_off",
        )
        _, decision = runner.assess(p, None)
        if not decision.accepted:
            continue
        order, created = book.propose(
            key=key,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=decision.quantity,
            at=at,
            idempotency_key=p.idempotency_key,
            strategy="kill-switch",
            purpose="square_off",
        )
        if created:
            book.transition(order, OrderState.RISK_CHECKED, at, outcome=decision.outcome.value)
            book.submit(order, at)
            made.append(order.id)
        session.add(
            controls.risk_check_row(
                context="PAPER",
                account_id=acct.id,
                proposal=p,
                state=runner.risk_state(p),
                decision=decision,
                order_id=order.id,
            )
        )
    return made
