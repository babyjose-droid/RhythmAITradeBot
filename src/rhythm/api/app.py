"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from rhythm import __version__
from rhythm.api.routes import system
from rhythm.api.state import AppState
from rhythm.audit import ActorType, AuditLog
from rhythm.config import Settings, get_settings
from rhythm.core.clock import Clock, SystemClock
from rhythm.core.logging import configure_logging, get_logger
from rhythm.db.session import create_db_engine, create_session_factory, session_scope

log = get_logger(__name__)


def _audit_lifecycle(state: AppState, event_type: str) -> None:
    """Best-effort lifecycle audit: a DB outage must not prevent liveness."""
    try:
        with session_scope(state.session_factory) as s:
            state.audit.append(
                s,
                event_type=event_type,
                actor_type=ActorType.SYSTEM,
                actor_id="api",
                payload={
                    "version": __version__,
                    "trading_mode": state.settings.trading_mode.value,
                    "features": state.settings.features.model_dump(),
                },
            )
    except Exception:
        log.exception("lifecycle_audit_failed", event_type=event_type)


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    clock: Clock | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    settings = settings or get_settings()  # raises on unsafe config (e.g. LIVE)
    if configure_logs:
        configure_logging(settings.log_level, json=settings.log_json)
    engine = engine or create_db_engine(settings.database_url.get_secret_value())
    clock = clock or SystemClock()
    state = AppState(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        clock=clock,
        audit=AuditLog(environment=settings.environment.value, clock=clock),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "startup",
            version=__version__,
            environment=settings.environment.value,
            trading_mode=settings.trading_mode.value,
        )
        _audit_lifecycle(state, "system.started")
        yield
        _audit_lifecycle(state, "system.stopped")
        log.info("shutdown")

    app = FastAPI(
        title="Rhythm AI Trader API",
        version=__version__,
        description="Research and paper-trading API. Live trading is not available.",
        lifespan=lifespan,
    )
    app.state.rhythm = state

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id[:64])
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request_id[:64]
        response.headers["x-trading-mode"] = settings.trading_mode.value
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return response

    @app.exception_handler(OperationalError)
    async def database_unavailable(_: Request, exc: OperationalError) -> JSONResponse:
        log.error("database_unavailable", error_type=type(exc.orig).__name__)
        return JSONResponse(
            status_code=503,
            content={"type": "about:blank", "title": "Database unavailable", "status": 503},
            media_type="application/problem+json",
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"type": "about:blank", "title": "Internal Server Error", "status": 500},
            media_type="application/problem+json",
        )

    app.include_router(system.router)
    return app
