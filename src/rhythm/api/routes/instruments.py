"""Instrument registry endpoints (read + scrip-master sync from the configured broker)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from rhythm.api.deps import SessionDep, StateDep, parse_key, problem
from rhythm.broker.base import BrokerAuthError, BrokerError
from rhythm.core.clock import to_exchange_local
from rhythm.core.market import Exchange
from rhythm.db.models import Instrument, InstrumentSnapshot
from rhythm.db.session import session_scope
from rhythm.instruments.registry import MockDataNotAllowedError, apply_snapshot, instruments_as_of
from rhythm.instruments.snapshot import SnapshotRejectedError

router = APIRouter(prefix="/v1/instruments", tags=["instruments"])


class InstrumentOut(BaseModel):
    exchange: str
    symbol: str
    name: str | None
    isin: str | None
    instrument_type: str
    tick_size: str
    lot_size: int
    source: str
    source_code: str
    is_mock: bool
    valid_from: date
    valid_to: date | None

    @classmethod
    def of(cls, r: Instrument) -> InstrumentOut:
        return cls(
            exchange=r.exchange,
            symbol=r.symbol,
            name=r.name,
            isin=r.isin,
            instrument_type=r.instrument_type,
            tick_size=str(r.tick_size.normalize()),
            lot_size=r.lot_size,
            source=r.source,
            source_code=r.source_code,
            is_mock=r.is_mock,
            valid_from=r.valid_from,
            valid_to=r.valid_to,
        )


class SnapshotOut(BaseModel):
    id: int
    source: str
    exchange: str
    as_of_date: date
    received_at: datetime
    row_count: int
    status: str
    summary: dict[str, Any]


class SyncOut(BaseModel):
    snapshot_id: int
    status: str
    summary: dict[str, Any]
    already_applied: bool
    source: str
    is_mock: bool


@router.get("", response_model=list[InstrumentOut])
def list_instruments(
    state: StateDep,
    session: SessionDep,
    exchange: Exchange = Exchange.NSE,
    as_of: date | None = None,
    q: str | None = Query(None, max_length=50),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[InstrumentOut]:
    on = as_of or to_exchange_local(state.clock.now()).date()
    rows = instruments_as_of(session, exchange, on, search=q, limit=limit, offset=offset)
    return [InstrumentOut.of(r) for r in rows]


@router.get("/snapshots", response_model=list[SnapshotOut])
def list_snapshots(session: SessionDep, limit: int = Query(20, ge=1, le=200)) -> list[SnapshotOut]:
    rows = session.execute(
        select(InstrumentSnapshot).order_by(InstrumentSnapshot.id.desc()).limit(limit)
    ).scalars()
    return [
        SnapshotOut(
            id=r.id,
            source=r.source,
            exchange=r.exchange,
            as_of_date=r.as_of_date,
            received_at=r.received_at,
            row_count=r.row_count,
            status=r.status,
            summary=r.summary,
        )
        for r in rows
    ]


@router.post("/sync", response_model=SyncOut)
def sync_scrip_master(state: StateDep, exchange: Exchange = Exchange.NSE) -> SyncOut:
    """Fetch the scrip master from the configured broker adapter and apply it as a snapshot."""
    if state.broker is None:
        raise problem(status.HTTP_503_SERVICE_UNAVAILABLE, "no broker adapter configured")
    try:
        records = state.broker.get_scrip_master(exchange)
    except BrokerAuthError as exc:
        raise problem(status.HTTP_503_SERVICE_UNAVAILABLE, f"broker login required: {exc}") from exc
    except BrokerError as exc:
        raise problem(status.HTTP_503_SERVICE_UNAVAILABLE, f"broker unavailable: {exc}") from exc
    as_of = to_exchange_local(state.clock.now()).date()
    # Separate transaction so a rejection record survives the error response.
    with session_scope(state.session_factory) as s:
        try:
            result = apply_snapshot(
                s,
                records=records,
                exchange=exchange,
                source=state.broker.origin,
                as_of=as_of,
                clock=state.clock,
                audit=state.audit,
                environment=state.settings.environment,
                actor_id=state.settings.local_actor_id,
            )
        except MockDataNotAllowedError as exc:
            raise problem(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except SnapshotRejectedError as rejected:
            error: SnapshotRejectedError | None = rejected
        else:
            error = None
    if error is not None:
        raise problem(422, f"snapshot rejected: {error.reason}")
    return SyncOut(
        snapshot_id=result.snapshot_id,
        status=result.status,
        summary=result.summary,
        already_applied=result.already_applied,
        source=state.broker.origin.value,
        is_mock=state.broker.origin.value == "MOCK",
    )


@router.get("/{exchange}/{symbol}", response_model=list[InstrumentOut])
def instrument_history(exchange: str, symbol: str, session: SessionDep) -> list[InstrumentOut]:
    """All versions of an instrument, newest first."""
    key = parse_key(exchange, symbol)
    rows = session.execute(
        select(Instrument)
        .where(Instrument.exchange == key.exchange.value, Instrument.symbol == key.symbol)
        .order_by(Instrument.valid_from.desc())
    ).scalars()
    out = [InstrumentOut.of(r) for r in rows]
    if not out:
        raise problem(status.HTTP_404_NOT_FOUND, f"{key} not found")
    return out
