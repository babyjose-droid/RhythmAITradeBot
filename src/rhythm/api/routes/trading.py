"""Strategies, backtests, PAPER accounts/orders/fills and signal (candidate) endpoints.

Every PAPER response carries ``account_kind: PAPER``; signals are candidates, not orders.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from rhythm.api.deps import SessionDep, StateDep, parse_key
from rhythm.api.idempotency import IdempotencyKey, run_idempotent
from rhythm.audit import ActorType
from rhythm.backtest.engine import (
    BacktestConfig,
    BacktestInputError,
    BacktestResult,
    Split,
    run_backtest,
    run_splits,
)
from rhythm.backtest.store import FULL, save_run
from rhythm.core.market import DataOrigin, InstrumentKey
from rhythm.db.models import (
    BacktestRun,
    BacktestTrade,
    FillRow,
    OrderEventRow,
    OrderRow,
    PaperAccount,
    PaperPosition,
    StrategySignalRow,
)
from rhythm.execution.book import ACCOUNT_KIND
from rhythm.instruments import watchlists as wl_svc
from rhythm.marketdata.bars import get_bars
from rhythm.marketdata.provider import MarketDataUnavailableError
from rhythm.marketdata.types import Bar, Interval
from rhythm.orders.state import OrderState, display_category
from rhythm.paper import service as paper
from rhythm.portfolio import service as portfolio
from rhythm.strategies.base import params_hash
from rhythm.strategies.intraday_orb import STRATEGIES, IntradayOrbVwap

router = APIRouter(tags=["trading"])


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except paper.AccountNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except wl_svc.NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except paper.PaperTradingDisabledError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (BacktestInputError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except MarketDataUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc


def _strategy(name: str) -> IntradayOrbVwap:
    if name not in STRATEGIES:
        raise HTTPException(404, f"unknown strategy {name!r}")
    return STRATEGIES[name]()


# ---------------------------------------------------------------------------- strategies
@router.get("/v1/strategies")
def strategies() -> list[dict[str, Any]]:
    out = []
    for cls in STRATEGIES.values():
        s = cls()
        out.append(
            {
                "name": s.name,
                "version": s.version,
                "horizon": s.horizon.value,
                "params": s.params,
                "params_hash": params_hash(s.params),
                "description": (cls.__doc__ or "").strip().splitlines()[0],
            }
        )
    return out


# ---------------------------------------------------------------------------- backtests
class SplitIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    start: datetime
    end: datetime


class BacktestIn(BaseModel):
    strategy: str = "intraday_orb_vwap"
    symbols: list[str] = Field(min_length=1, max_length=50)
    start: datetime
    end: datetime
    initial_capital: Decimal = Field(default=Decimal(1_000_000), gt=0)
    data_source: Literal["db", "provider"] = "db"
    splits: list[SplitIn] = Field(default_factory=list, max_length=10)


def _aware(*dts: datetime) -> None:
    if any(d.tzinfo is None for d in dts):
        raise HTTPException(422, "timestamps must include a timezone")


@router.post("/v1/backtests", status_code=201)
def create_backtest(body: BacktestIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    strat = _strategy(body.strategy)
    _aware(body.start, body.end, *[x for s in body.splits for x in (s.start, s.end)])
    keys = tuple(InstrumentKey.parse(s) if ":" in s else parse_key("NSE", s) for s in body.symbols)
    cfg = BacktestConfig(
        symbols=keys, start=body.start, end=body.end, initial_capital=body.initial_capital
    )
    bars: dict[InstrumentKey, list[Bar]] = {}
    with _errors():
        for k in keys:
            if body.data_source == "db":
                bars[k] = [
                    Bar(
                        key=k,
                        interval=Interval.M5,
                        start=r.bar_start,
                        open=r.open,
                        high=r.high,
                        low=r.low,
                        close=r.close,
                        volume=r.volume,
                        source=DataOrigin(r.source),
                        received_ts=r.received_at,
                    )
                    for r in get_bars(session, k, Interval.M5, body.start, body.end, limit=200_000)
                ]
            else:
                if state.market_data is None:
                    raise HTTPException(503, "no market data provider configured")
                bars[k] = list(state.market_data.bars(k, Interval.M5, body.start, body.end))
        if not any(bars.values()):
            raise HTTPException(422, "no 5m bars found for the requested symbols/period")
        results: dict[str, BacktestResult] = {
            FULL: run_backtest(strat, cfg, bars, calendar=state.calendar)
        }
        if body.splits:
            results |= run_splits(
                strat,
                cfg,
                bars,
                [Split(s.name, s.start, s.end) for s in body.splits],
                calendar=state.calendar,
            )
        run = save_run(
            session,
            strategy=strat,
            cfg=cfg,
            results=results,
            audit=state.audit,
            actor_id=state.settings.local_actor_id,
        )
    return _run_out(run) | {"trades": len(results[FULL].trades)}


def _run_out(r: BacktestRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "created_at": r.created_at,
        "strategy": r.strategy,
        "version": r.version,
        "params_hash": r.params_hash,
        "symbols": r.symbols,
        "interval": r.interval,
        "start": r.start_at,
        "end": r.end_at,
        "initial_capital": str(r.initial_capital),
        "data_is_mock": r.data_is_mock,
        "data_hash": r.data_hash,
        "run_hash": r.run_hash,
        "cost_model": r.cost_model,
        "metrics": r.metrics,
        "splits": r.splits,
        "bars_processed": r.bars_processed,
        "signals_count": r.signals_count,
    }


@router.get("/v1/backtests")
def list_backtests(
    session: SessionDep, limit: int = Query(20, ge=1, le=200)
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(BacktestRun).order_by(BacktestRun.id.desc()).limit(limit)
    ).scalars()
    return [_run_out(r) for r in rows]


@router.get("/v1/backtests/{run_id}")
def get_backtest(run_id: int, session: SessionDep) -> dict[str, Any]:
    r = session.get(BacktestRun, run_id)
    if r is None:
        raise HTTPException(404, f"backtest {run_id} not found")
    trades = session.execute(
        select(BacktestTrade)
        .where(BacktestTrade.run_id == run_id)
        .order_by(BacktestTrade.split, BacktestTrade.seq)
    ).scalars()
    return _run_out(r) | {
        "params": r.params,
        "fill_model": r.fill_model,
        "limits": r.limits,
        "trade_list": [
            {
                "split": t.split,
                "instrument": f"{t.exchange}:{t.symbol}",
                "entry_at": t.entry_at,
                "exit_at": t.exit_at,
                "quantity": t.quantity,
                "entry_price": str(t.entry_price),
                "exit_price": str(t.exit_price),
                "net_pnl": str(t.net_pnl),
                "charges": str(t.charges),
                "r_multiple": t.r_multiple,
                "exit_reason": t.exit_reason,
            }
            for t in trades
        ],
    }


# ---------------------------------------------------------------------------- paper accounts
class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    starting_cash: Decimal = Field(default=Decimal(1_000_000), gt=0, le=Decimal(10**12))


def _paper(response: Response) -> None:
    response.headers["x-account-kind"] = ACCOUNT_KIND


@router.post("/v1/paper/accounts", status_code=201)
def create_account(
    body: AccountIn, state: StateDep, session: SessionDep, response: Response
) -> dict[str, Any]:
    _paper(response)
    if session.execute(select(PaperAccount.id).where(PaperAccount.name == body.name)).first():
        raise HTTPException(409, f"paper account {body.name!r} exists")
    a = paper.create_account(
        session,
        state.audit,
        name=body.name,
        starting_cash=body.starting_cash,
        actor_id=state.settings.local_actor_id,
    )
    return {"id": a.id, "name": a.name, "account_kind": ACCOUNT_KIND, "cash": str(a.cash)}


@router.get("/v1/paper/accounts")
def list_accounts(session: SessionDep, response: Response) -> list[dict[str, Any]]:
    _paper(response)
    return [
        {
            "id": a.id,
            "name": a.name,
            "account_kind": ACCOUNT_KIND,
            "cash": str(a.cash),
            "starting_cash": str(a.starting_cash),
        }
        for a in session.execute(select(PaperAccount).order_by(PaperAccount.id)).scalars()
    ]


@router.get("/v1/paper/accounts/{account_id}")
def account_detail(account_id: int, session: SessionDep, response: Response) -> dict[str, Any]:
    _paper(response)
    with _errors():
        a = paper.get_account(session, account_id)
    positions = session.execute(
        select(PaperPosition).where(
            PaperPosition.account_id == account_id, PaperPosition.quantity > 0
        )
    ).scalars()
    realized = session.execute(
        select(PaperPosition.realized_pnl).where(PaperPosition.account_id == account_id)
    ).scalars()
    return {
        "id": a.id,
        "name": a.name,
        "account_kind": ACCOUNT_KIND,
        "starting_cash": str(a.starting_cash),
        "cash": str(a.cash),
        "realized_pnl": str(sum(realized, Decimal(0))),
        "positions": [
            {
                "instrument": f"{p.exchange}:{p.symbol}",
                "quantity": p.quantity,
                "average_price": str(p.average_price),
                "stop": None if p.stop is None else str(p.stop),
                "target": None if p.target is None else str(p.target),
                "opened_at": p.opened_at,
            }
            for p in positions
        ],
        "state": a.state,
        "ledgers": {
            lg.horizon: str(lg.allocated_capital)
            for lg in portfolio.ledgers(session, account_id).values()
        },
    }


def _order_out(o: OrderRow) -> dict[str, Any]:
    return {
        "id": o.id,
        "account_kind": o.account_kind,
        "instrument": f"{o.exchange}:{o.symbol}",
        "side": o.side,
        "order_type": o.order_type,
        "purpose": o.purpose,
        "horizon": o.horizon,
        "venue_order_id": o.venue_order_id,
        "quantity": o.quantity,
        "filled_quantity": o.filled_quantity,
        "average_fill_price": None if o.average_fill_price is None else str(o.average_fill_price),
        "limit_price": None if o.limit_price is None else str(o.limit_price),
        "stop_price": None if o.stop_price is None else str(o.stop_price),
        "state": o.state,
        "display": display_category(OrderState(o.state), o.filled_quantity).value,
        "reason": o.reason,
        "data_is_mock": o.data_is_mock,
        "created_at": o.created_at,
        "updated_at": o.updated_at,
    }


@router.get("/v1/paper/accounts/{account_id}/orders")
def orders(
    account_id: int, session: SessionDep, response: Response, limit: int = Query(200, ge=1, le=2000)
) -> list[dict[str, Any]]:
    _paper(response)
    rows = session.execute(
        select(OrderRow)
        .where(OrderRow.account_id == account_id)
        .order_by(OrderRow.created_at.desc(), OrderRow.id)
        .limit(limit)
    ).scalars()
    return [_order_out(o) for o in rows]


@router.get("/v1/paper/orders/{order_id}/events")
def order_events(order_id: str, session: SessionDep, response: Response) -> list[dict[str, Any]]:
    _paper(response)
    rows = session.execute(
        select(OrderEventRow).where(OrderEventRow.order_id == order_id).order_by(OrderEventRow.id)
    ).scalars()
    out = [{"from": e.from_state, "to": e.to_state, "at": e.at, "detail": e.detail} for e in rows]
    if not out:
        raise HTTPException(404, f"order {order_id} not found")
    return out


@router.post("/v1/paper/orders/{order_id}/cancel")
def cancel_order(
    order_id: str,
    state: StateDep,
    session: SessionDep,
    response: Response,
    idem: IdempotencyKey = None,
) -> Any:
    _paper(response)

    def produce() -> dict[str, Any]:
        o = session.get(OrderRow, order_id, with_for_update=True)
        if o is None:
            raise HTTPException(404, f"order {order_id} not found")
        if OrderState(o.state) not in (OrderState.BROKER_ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
            raise HTTPException(409, f"order in state {o.state} cannot be cancelled")
        now = state.clock.now()
        session.add(
            OrderEventRow(
                order_id=o.id,
                from_state=o.state,
                to_state="CANCELLED",
                at=now,
                detail={"reason": "user cancel"},
            )
        )
        o.state, o.reason, o.updated_at = "CANCELLED", "user cancel", now
        state.audit.append(
            session,
            event_type="paper.order_cancelled",
            actor_type=ActorType.USER,
            actor_id=state.settings.local_actor_id,
            subject_type="order",
            subject_id=o.id,
            payload={"account_kind": ACCOUNT_KIND},
        )
        session.flush()
        return _order_out(o)

    return run_idempotent(
        session,
        key=idem,
        endpoint=f"POST /v1/paper/orders/{order_id}/cancel",
        body={},
        produce=produce,
        required=True,
        headers={"x-account-kind": ACCOUNT_KIND},
    )


@router.get("/v1/paper/accounts/{account_id}/fills")
def fills(
    account_id: int, session: SessionDep, response: Response, limit: int = Query(200, ge=1, le=2000)
) -> list[dict[str, Any]]:
    _paper(response)
    rows = session.execute(
        select(FillRow)
        .where(FillRow.account_id == account_id)
        .order_by(FillRow.at.desc())
        .limit(limit)
    ).scalars()
    return [
        {
            "id": f.id,
            "account_kind": f.account_kind,
            "order_id": f.order_id,
            "instrument": f"{f.exchange}:{f.symbol}",
            "side": f.side,
            "quantity": f.quantity,
            "price": str(f.price),
            "charges_total": str(f.charges_total),
            "charges": f.charges,
            "at": f.at,
        }
        for f in rows
    ]


class CycleIn(BaseModel):
    watchlist_id: int
    strategy: str = "intraday_orb_vwap"


def _cycle(
    body: CycleIn, state: StateDep, session: SessionDep, account_id: int | None
) -> dict[str, Any]:
    if state.market_data is None:
        raise HTTPException(503, "no market data provider configured")
    strat = _strategy(body.strategy)
    with _errors():
        wl = wl_svc.get_watchlist(session, body.watchlist_id)
        keys = [parse_key(i.exchange, i.symbol) for i in wl.items]
        res = paper.run_cycle(
            session,
            keys=keys,
            provider=state.market_data,
            strategy=strat,
            settings=state.settings,
            clock=state.clock,
            calendar=state.calendar,
            audit=state.audit,
            actor_id=state.settings.local_actor_id,
            account_id=account_id,
            policy=state.freshness.policy,
        )
    return res.__dict__ | {"account_kind": ACCOUNT_KIND if account_id is not None else None}


@router.post("/v1/paper/accounts/{account_id}/cycle")
def paper_cycle(
    account_id: int,
    body: CycleIn,
    state: StateDep,
    session: SessionDep,
    response: Response,
    idem: IdempotencyKey = None,
) -> Any:
    """Run one PAPER cycle. Requires an Idempotency-Key: a retried request is not re-run."""
    _paper(response)
    return run_idempotent(
        session,
        key=idem,
        endpoint=f"POST /v1/paper/accounts/{account_id}/cycle",
        body=body.model_dump(),
        produce=lambda: _cycle(body, state, session, account_id),
        required=True,
        headers={"x-account-kind": ACCOUNT_KIND},
    )


@router.post("/v1/research/cycle")
def research_cycle(body: CycleIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    """Evaluate the strategy and record candidates only. Never creates an order."""
    return _cycle(body, state, session, None)


@router.get("/v1/signals")
def signals(
    session: SessionDep,
    account_id: int | None = None,
    context: Literal["PAPER", "RESEARCH"] | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    stmt = select(StrategySignalRow).order_by(StrategySignalRow.id.desc()).limit(limit)
    if account_id is not None:
        stmt = stmt.where(StrategySignalRow.account_id == account_id)
    if context:
        stmt = stmt.where(StrategySignalRow.context == context)
    return [
        {
            "id": s.id,
            "kind": "CANDIDATE",
            "context": s.context,
            "instrument": f"{s.exchange}:{s.symbol}",
            "strategy": f"{s.strategy}@{s.version}",
            "signal_type": s.signal_type,
            "decision_state": s.decision_state,
            "decided_at": s.decided_at,
            "ref_price": str(s.ref_price),
            "stop": None if s.stop is None else str(s.stop),
            "target": None if s.target is None else str(s.target),
            "reasons": s.reasons,
            "risk": s.risk,
            "order_id": s.order_id,
            "data_is_mock": s.data_is_mock,
        }
        for s in session.execute(stmt).scalars()
    ]
