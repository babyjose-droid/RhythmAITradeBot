"""Market-data endpoints: quotes with freshness, stored bars, backfill, freshness, quality."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from rhythm.api.deps import SessionDep, StateDep, parse_key
from rhythm.core.market import Horizon
from rhythm.db.models import DataQualityEvent
from rhythm.instruments import watchlists as wl_svc
from rhythm.instruments.registry import MockDataNotAllowedError
from rhythm.marketdata.bars import get_bars, upsert_bars
from rhythm.marketdata.freshness import evaluate_quote
from rhythm.marketdata.provider import MarketDataUnavailableError
from rhythm.marketdata.types import Interval

router = APIRouter(tags=["marketdata"])


class FreshnessOut(BaseModel):
    status: str
    usable_for_new_orders: bool
    age_seconds: float | None
    as_of: datetime | None
    reasons: list[str]


class QuoteOut(BaseModel):
    instrument: str
    ltp: str
    bid: str | None
    ask: str | None
    volume: int | None
    source: str
    is_mock: bool
    source_ts: datetime
    received_ts: datetime
    freshness: FreshnessOut


class BarOut(BaseModel):
    start: datetime
    open: str
    high: str
    low: str
    close: str
    volume: int
    source: str
    is_mock: bool
    received_at: datetime


class InstrumentFreshnessOut(BaseModel):
    instrument: str
    horizon: str
    source: str
    is_mock: bool
    freshness: FreshnessOut


class QualityEventOut(BaseModel):
    id: int
    occurred_at: datetime
    source: str
    kind: str
    severity: str
    instrument: str | None
    detail: dict[str, Any]


def _fresh(
    status_: Any, age: float | None, as_of: datetime | None, reasons: list[str]
) -> FreshnessOut:
    return FreshnessOut(
        status=status_.value,
        usable_for_new_orders=status_.usable_for_new_orders,
        age_seconds=None if age is None else round(age, 3),
        as_of=as_of,
        reasons=reasons,
    )


@router.get("/v1/marketdata/quote/{exchange}/{symbol}", response_model=QuoteOut)
def quote(exchange: str, symbol: str, state: StateDep) -> QuoteOut:
    key = parse_key(exchange, symbol)
    if state.market_data is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "no market data provider configured"
        )
    try:
        q = state.market_data.latest_quote(key)
    except MarketDataUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if q is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no quote for {key}")
    f = evaluate_quote(q, state.clock.now(), state.calendar, state.freshness.policy)
    return QuoteOut(
        instrument=str(key),
        ltp=str(q.ltp),
        bid=None if q.bid is None else str(q.bid),
        ask=None if q.ask is None else str(q.ask),
        volume=q.volume,
        source=q.source.value,
        is_mock=q.is_mock,
        source_ts=q.source_ts,
        received_ts=q.received_ts,
        freshness=_fresh(f.status, f.age_seconds, f.as_of, f.reasons),
    )


@router.get("/v1/marketdata/bars/{exchange}/{symbol}", response_model=list[BarOut])
def bars(
    exchange: str,
    symbol: str,
    state: StateDep,
    session: SessionDep,
    interval: Interval = Interval.D1,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(1000, ge=1, le=5000),
) -> list[BarOut]:
    key = parse_key(exchange, symbol)
    end = end or state.clock.now()
    start = start or end - timedelta(days=365)
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "start/end must include a timezone")
    rows = get_bars(session, key, interval, start, end, limit=limit)
    return [
        BarOut(
            start=r.bar_start,
            open=str(r.open),
            high=str(r.high),
            low=str(r.low),
            close=str(r.close),
            volume=r.volume,
            source=r.source,
            is_mock=r.is_mock,
            received_at=r.received_at,
        )
        for r in rows
    ]


@router.post("/v1/marketdata/bars/{exchange}/{symbol}/backfill")
def backfill(
    exchange: str,
    symbol: str,
    state: StateDep,
    session: SessionDep,
    interval: Interval = Interval.D1,
    days: int = Query(30, ge=1, le=3650),
) -> dict[str, Any]:
    """Pull bars from the configured provider and store them (validated, provenance-tagged)."""
    key = parse_key(exchange, symbol)
    if state.market_data is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "no market data provider configured"
        )
    end = state.clock.now()
    try:
        fetched = state.market_data.bars(key, interval, end - timedelta(days=days), end)
        result = upsert_bars(
            session, fetched, clock=state.clock, environment=state.settings.environment
        )
    except MarketDataUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except MockDataNotAllowedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return {
        "instrument": str(key),
        "interval": interval.value,
        "source": state.market_data.origin.value,
        "is_mock": state.market_data.origin.value == "MOCK",
        "written": result.written,
        "rejected": result.rejected,
    }


@router.get("/v1/marketdata/freshness", response_model=list[InstrumentFreshnessOut])
def freshness(
    state: StateDep,
    session: SessionDep,
    watchlist_id: int,
    horizon: Horizon | None = None,
) -> list[InstrumentFreshnessOut]:
    try:
        wl = wl_svc.get_watchlist(session, watchlist_id)
    except wl_svc.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if horizon is None:
        if wl.horizon == "ANY":
            raise HTTPException(422, "watchlist horizon is ANY; pass ?horizon=")
        horizon = Horizon(wl.horizon)
    keys = [parse_key(i.exchange, i.symbol) for i in wl.items]
    results = state.freshness.check(session, keys, horizon)
    return [
        InstrumentFreshnessOut(
            instrument=str(r.key),
            horizon=r.horizon.value,
            source=r.source,
            is_mock=r.is_mock,
            freshness=_fresh(
                r.result.status, r.result.age_seconds, r.result.as_of, r.result.reasons
            ),
        )
        for r in results
    ]


@router.get("/v1/data-quality/events", response_model=list[QualityEventOut])
def quality_events(
    session: SessionDep,
    limit: int = Query(100, ge=1, le=1000),
    kind: str | None = None,
) -> list[QualityEventOut]:
    stmt = select(DataQualityEvent).order_by(DataQualityEvent.id.desc()).limit(limit)
    if kind:
        stmt = stmt.where(DataQualityEvent.kind == kind.upper())
    return [
        QualityEventOut(
            id=e.id,
            occurred_at=e.occurred_at,
            source=e.source,
            kind=e.kind,
            severity=e.severity,
            instrument=f"{e.exchange}:{e.symbol}" if e.symbol else None,
            detail=e.detail,
        )
        for e in session.execute(stmt).scalars()
    ]
