"""Corporate-event calendar, company fundamentals, and multi-day theses (swing, long-term).

Swing cycles themselves use ``POST /v1/paper/accounts/{id}/cycle`` (or the research cycle)
with ``strategy: swing_trend_pullback``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from rhythm.api.deps import SessionDep, StateDep, parse_key
from rhythm.api.idempotency import IdempotencyKey, run_idempotent
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Horizon, InstrumentKey
from rhythm.db.models import ThesisEventRow, ThesisRow
from rhythm.events import store as event_store
from rhythm.events.types import CorporateEvent, EventKind
from rhythm.fundamentals import store as fundamentals_store
from rhythm.fundamentals.types import derive, known_as_of
from rhythm.instruments import watchlists as wl_svc
from rhythm.instruments.registry import MockDataNotAllowedError
from rhythm.marketdata.provider import MarketDataUnavailableError
from rhythm.paper import theses

router = APIRouter(tags=["swing"])


def _watchlist_keys(session: SessionDep, watchlist_id: int) -> list[InstrumentKey]:
    try:
        wl = wl_svc.get_watchlist(session, watchlist_id)
    except wl_svc.NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return [parse_key(i.exchange, i.symbol) for i in wl.items]


# ---------------------------------------------------------------------------- events
class SyncIn(BaseModel):
    watchlist_id: int


@router.post("/v1/events/sync")
def sync_events(
    body: SyncIn, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    if state.events is None:
        raise HTTPException(503, "no event provider configured (RHYTHM_EVENT_PROVIDER)")
    provider = state.events
    keys = _watchlist_keys(session, body.watchlist_id)

    def produce() -> dict[str, Any]:
        try:
            added = event_store.sync(
                session,
                provider,
                keys,
                now=state.clock.now(),
                environment=state.settings.environment,
                audit=state.audit,
                actor_id=state.settings.local_actor_id,
            )
        except MockDataNotAllowedError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"source": provider.origin.value, "added": added, "instruments": len(keys)}

    return run_idempotent(
        session,
        key=idem,
        endpoint="POST /v1/events/sync",
        body=body.model_dump(),
        produce=produce,
        required=False,
    )


class EventIn(BaseModel):
    instrument: str
    kind: EventKind
    event_date: date
    known_at: datetime | None = None
    source_ref: str = Field(min_length=3, max_length=120)
    note: str = Field(default="", max_length=300)


@router.post("/v1/events", status_code=201)
def add_event(body: EventIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    """A user-entered event (source MANUAL). Adds information; never counts as coverage."""
    now = state.clock.now()
    known = body.known_at or now
    if known.tzinfo is None:
        raise HTTPException(422, "known_at must include a timezone")
    if known > now:
        raise HTTPException(422, "known_at cannot be in the future")
    try:
        key = InstrumentKey.parse(body.instrument)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    ev = CorporateEvent(
        key=key,
        kind=body.kind,
        event_date=body.event_date,
        known_at=known,
        source="MANUAL",
        source_ref=body.source_ref,
        note=body.note,
    )
    added = event_store.add_manual(
        session, ev, audit=state.audit, actor_id=state.settings.local_actor_id
    )
    return {"added": added, "instrument": str(key), "kind": body.kind.value}


@router.get("/v1/events")
def list_events(
    watchlist_id: int,
    state: StateDep,
    session: SessionDep,
    days: int = Query(60, ge=1, le=366),
) -> dict[str, Any]:
    keys = _watchlist_keys(session, watchlist_id)
    now = state.clock.now()
    today = now.astimezone(EXCHANGE_TZ).date()
    rows = event_store.list_events(session, keys, today, today + timedelta(days=days))
    coverage = {str(k): event_store.last_sync(session, k, now) for k in keys}
    return {
        "events": [
            {
                "instrument": f"{r.exchange}:{r.symbol}",
                "kind": r.kind,
                "event_date": r.event_date,
                "days_away": (r.event_date - today).days,
                "known_at": r.known_at,
                "source": r.source,
                "source_ref": r.source_ref,
                "note": r.note,
                "is_mock": r.is_mock,
            }
            for r in rows
        ],
        "last_sync": coverage,
    }


# ---------------------------------------------------------------------------- theses
@router.get("/v1/theses")
def list_theses(
    session: SessionDep,
    account_id: int,
    status: Literal["PENDING_ENTRY", "OPEN", "INVALIDATED", "CLOSED"] | None = None,
    horizon: Horizon | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    stmt = (
        select(ThesisRow)
        .where(ThesisRow.account_id == account_id)
        .order_by(ThesisRow.id.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(ThesisRow.status == status)
    if horizon is not None:
        stmt = stmt.where(ThesisRow.horizon == horizon.value)
    return [theses.thesis_out(t) for t in session.execute(stmt).scalars()]


@router.get("/v1/theses/{thesis_id}")
def get_thesis(thesis_id: int, session: SessionDep) -> dict[str, Any]:
    th = session.get(ThesisRow, thesis_id)
    if th is None:
        raise HTTPException(404, f"thesis {thesis_id} not found")
    events = list(
        session.execute(
            select(ThesisEventRow)
            .where(ThesisEventRow.thesis_id == thesis_id)
            .order_by(ThesisEventRow.id)
        ).scalars()
    )
    return theses.thesis_out(th, events)


# ---------------------------------------------------------------------------- fundamentals
@router.post("/v1/fundamentals/sync")
def sync_fundamentals(
    body: SyncIn, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    if state.fundamentals is None:
        raise HTTPException(
            503, "no fundamentals provider configured (RHYTHM_FUNDAMENTALS_PROVIDER)"
        )
    provider = state.fundamentals
    keys = _watchlist_keys(session, body.watchlist_id)

    def produce() -> dict[str, Any]:
        try:
            added = fundamentals_store.sync(
                session,
                provider,
                keys,
                now=state.clock.now(),
                environment=state.settings.environment,
                audit=state.audit,
                actor_id=state.settings.local_actor_id,
            )
        except MockDataNotAllowedError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"source": provider.origin.value, "added": added, "instruments": len(keys)}

    return run_idempotent(
        session,
        key=idem,
        endpoint="POST /v1/fundamentals/sync",
        body=body.model_dump(),
        produce=produce,
        required=False,
    )


@router.get("/v1/fundamentals/{exchange}/{symbol}")
def get_fundamentals(
    exchange: str,
    symbol: str,
    state: StateDep,
    session: SessionDep,
    quarters: int = Query(8, ge=1, le=40),
) -> dict[str, Any]:
    """Published quarters (point-in-time as of now) and derived metrics with formulas."""
    key = parse_key(exchange, symbol)
    now = state.clock.now()
    snaps = known_as_of(fundamentals_store.known(session, key, now), now)
    price: Decimal | None = None
    price_source = "unavailable"
    if state.market_data is not None:
        try:
            q = state.market_data.latest_quote(key)
        except MarketDataUnavailableError:
            q = None
        if q is not None:
            price, price_source = q.ltp, q.source.value
    derived = derive(snaps, price) if price is not None else {}
    return {
        "instrument": str(key),
        "as_of": now,
        "price": None if price is None else str(price),
        "price_source": price_source,
        "is_mock": any(s.is_mock for s in snaps),
        "quarters": [
            {
                "period_end": s.period_end,
                "known_at": s.known_at,
                "source": s.source,
                "source_ref": s.source_ref,
                "metrics": {k: str(v) for k, v in s.metrics.items()},
            }
            for s in snaps[-quarters:]
        ],
        "derived": {k: v.as_dict() for k, v in derived.items()},
    }
