"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from rhythm import __version__
from rhythm.api.routes import (
    instruments,
    marketdata,
    risk,
    swing,
    system,
    trading,
    watchlists,
)
from rhythm.api.state import AppState
from rhythm.audit import ActorType, AuditLog
from rhythm.broker.base import BrokerReadAdapter
from rhythm.broker.mock import MockBrokerAdapter
from rhythm.config import Settings, get_settings
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import Clock, SystemClock
from rhythm.core.logging import configure_logging, get_logger
from rhythm.db.session import create_db_engine, create_session_factory, session_scope
from rhythm.events.mock import MockEventProvider
from rhythm.events.types import EventProvider
from rhythm.marketdata.freshness import FreshnessPolicy
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.monitor import FreshnessMonitor
from rhythm.marketdata.provider import MarketDataProvider

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
                    "market_data_provider": state.settings.market_data_provider,
                    "broker_adapter": state.settings.broker_adapter,
                    "event_provider": state.settings.event_provider,
                },
            )
    except Exception:
        log.exception("lifecycle_audit_failed", event_type=event_type)


def build_market_data(
    settings: Settings, clock: Clock, calendar: SessionCalendar
) -> MarketDataProvider | None:
    if settings.market_data_provider == "mock":
        return MockMarketDataProvider(clock=clock, calendar=calendar)
    return None


def build_events(settings: Settings) -> EventProvider | None:
    if settings.event_provider == "mock":
        return MockEventProvider()
    return None


def build_broker(settings: Settings, clock: Clock) -> BrokerReadAdapter | None:
    if settings.broker_adapter == "mock":
        return MockBrokerAdapter(clock=clock)
    return None


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    clock: Clock | None = None,
    market_data: MarketDataProvider | None = None,
    broker: BrokerReadAdapter | None = None,
    calendar: SessionCalendar | None = None,
    events: EventProvider | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    settings = settings or get_settings()  # raises on unsafe config (e.g. LIVE)
    if configure_logs:
        configure_logging(settings.log_level, json=settings.log_json)
    engine = engine or create_db_engine(settings.database_url.get_secret_value())
    clock = clock or SystemClock()
    calendar = calendar or SessionCalendar()
    market_data = market_data or build_market_data(settings, clock, calendar)
    broker = broker or build_broker(settings, clock)
    state = AppState(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        clock=clock,
        audit=AuditLog(environment=settings.environment.value, clock=clock),
        calendar=calendar,
        market_data=market_data,
        broker=broker,
        freshness=FreshnessMonitor(
            provider=market_data,
            calendar=calendar,
            clock=clock,
            policy=FreshnessPolicy(
                intraday_max_age=timedelta(seconds=settings.freshness_intraday_max_age_seconds)
            ),
        ),
        events=events or build_events(settings),
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
    app.include_router(instruments.router)
    app.include_router(watchlists.router)
    app.include_router(marketdata.router)
    app.include_router(trading.router)
    app.include_router(risk.router)
    app.include_router(swing.router)
    return app
