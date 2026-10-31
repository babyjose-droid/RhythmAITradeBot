"""Health, mode and audit read endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from rhythm.api.health import check_database_and_clock, check_migrations, check_trading_mode
from rhythm.api.state import AppState
from rhythm.audit import AuditLog
from rhythm.config import LIVE_TRADING_IMPLEMENTED
from rhythm.core.clock import EXCHANGE_TZ_NAME, to_exchange_local
from rhythm.db.models import AuditEvent, SystemHealthEvent
from rhythm.db.session import session_scope

router = APIRouter()


def _state(request: Request) -> AppState:
    st: AppState = request.app.state.rhythm
    return st


class CheckOut(BaseModel):
    name: str
    ok: bool
    detail: dict[str, Any]


class ReadinessOut(BaseModel):
    status: str
    checks: list[CheckOut]


class ModeOut(BaseModel):
    environment: str
    trading_mode: str
    orders_permitted: bool
    live_trading_available: bool
    features: dict[str, bool]
    exchange_timezone: str
    server_time_utc: datetime
    server_time_exchange: datetime


class AuditEventOut(BaseModel):
    id: int
    occurred_at: datetime
    event_type: str
    actor_type: str
    actor_id: str
    subject_type: str | None
    subject_id: str | None
    payload: dict[str, Any]
    row_hash: str


class AuditVerifyOut(BaseModel):
    ok: bool
    rows_checked: int
    first_bad_id: int | None
    reason: str | None


@router.get("/health/live", tags=["health"])
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", tags=["health"], response_model=ReadinessOut)
def ready(request: Request, response: Response) -> ReadinessOut:
    st = _state(request)
    checks = [
        *check_database_and_clock(st.engine, st.clock, st.settings.max_clock_skew_seconds),
        check_migrations(st.engine),
        check_trading_mode(st.settings.trading_mode),
    ]
    ok = all(c.ok for c in checks)
    response.status_code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    st.record_readiness(ok, {c.name: c.ok for c in checks})
    return ReadinessOut(
        status="ready" if ok else "not_ready",
        checks=[CheckOut(name=c.name, ok=c.ok, detail=c.detail) for c in checks],
    )


@router.get("/v1/system/mode", tags=["system"], response_model=ModeOut)
def mode(request: Request) -> ModeOut:
    st = _state(request)
    now = st.clock.now()
    return ModeOut(
        environment=st.settings.environment.value,
        trading_mode=st.settings.trading_mode.value,
        orders_permitted=st.settings.orders_permitted,
        live_trading_available=LIVE_TRADING_IMPLEMENTED,
        features=st.settings.features.model_dump(),
        exchange_timezone=EXCHANGE_TZ_NAME,
        server_time_utc=now,
        server_time_exchange=to_exchange_local(now),
    )


@router.get("/v1/audit/events", tags=["audit"], response_model=list[AuditEventOut])
def audit_events(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    before_id: int | None = Query(None, ge=1),
) -> list[AuditEventOut]:
    st = _state(request)
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if before_id is not None:
        stmt = stmt.where(AuditEvent.id < before_id)
    with session_scope(st.session_factory) as s:
        rows = s.execute(stmt).scalars().all()
        return [
            AuditEventOut(
                id=r.id,
                occurred_at=r.occurred_at,
                event_type=r.event_type,
                actor_type=r.actor_type,
                actor_id=r.actor_id,
                subject_type=r.subject_type,
                subject_id=r.subject_id,
                payload=r.payload,
                row_hash=r.row_hash,
            )
            for r in rows
        ]


@router.get("/v1/audit/verify", tags=["audit"], response_model=AuditVerifyOut)
def audit_verify(request: Request) -> AuditVerifyOut:
    st = _state(request)
    with session_scope(st.session_factory) as s:
        res = AuditLog.verify(s)
    if not res.ok:
        with session_scope(st.session_factory) as s:
            s.add(
                SystemHealthEvent(
                    occurred_at=st.clock.now(),
                    component="audit",
                    status="down",
                    detail={"first_bad_id": res.first_bad_id, "reason": res.reason},
                )
            )
    return AuditVerifyOut(
        ok=res.ok, rows_checked=res.rows_checked, first_bad_id=res.first_bad_id, reason=res.reason
    )
