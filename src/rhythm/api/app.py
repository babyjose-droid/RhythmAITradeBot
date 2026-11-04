"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from rhythm import __version__
from rhythm.api.auth import access
from rhythm.api.routes import (
    auth as auth_routes,
)
from rhythm.api.routes import (
    broker as broker_routes,
)
from rhythm.api.routes import (
    instruments,
    marketdata,
    ops,
    research,
    risk,
    swing,
    system,
    trading,
    watchlists,
)
from rhythm.api.routes import (
    scheduler as scheduler_routes,
)
from rhythm.api.state import AppState
from rhythm.audit import ActorType, AuditLog
from rhythm.auth.service import AuthService
from rhythm.broker.base import BrokerReadAdapter
from rhythm.broker.mock import MockBrokerAdapter
from rhythm.broker.sharekhan.adapter import ActiveSession, SharekhanBroker
from rhythm.broker.sharekhan.client import SharekhanReadClient
from rhythm.brokerlink import service as broker_link
from rhythm.config import Settings, get_settings
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import Clock, SystemClock
from rhythm.core.logging import configure_logging, get_logger
from rhythm.db.session import create_db_engine, create_session_factory, session_scope
from rhythm.events.mock import MockEventProvider
from rhythm.events.types import EventProvider
from rhythm.fundamentals.mock import MockFundamentalsProvider
from rhythm.fundamentals.types import FundamentalsProvider
from rhythm.marketdata.freshness import FreshnessPolicy
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.monitor import FreshnessMonitor
from rhythm.marketdata.provider import MarketDataProvider
from rhythm.monitoring import metrics
from rhythm.notifications import service as notifications
from rhythm.notifications.dispatcher import WebhookDispatcher
from rhythm.research.providers import AnthropicProvider, MockResearchProvider, ResearchProvider
from rhythm.research.service import ResearchService

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
                    "fundamentals_provider": state.settings.fundamentals_provider,
                    "research_provider": state.settings.research_provider,
                    "research_gate_paper_entries": state.settings.research.gate_paper_entries,
                    "auth_enabled": state.settings.auth.enabled,
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


def build_fundamentals(
    settings: Settings, market: MarketDataProvider | None
) -> FundamentalsProvider | None:
    if settings.fundamentals_provider == "mock":
        mock = market if isinstance(market, MockMarketDataProvider) else MockMarketDataProvider()
        return MockFundamentalsProvider(mock.price_at)
    return None


def build_research_provider(settings: Settings) -> ResearchProvider | None:
    if settings.research_provider == "mock":
        return MockResearchProvider()
    if settings.research_provider == "anthropic":
        r = settings.research
        if r.api_key is None or r.model is None:  # also enforced by Settings
            raise ValueError("anthropic research provider needs an API key and a model")
        return AnthropicProvider(
            api_key=r.api_key.get_secret_value(),
            model=r.model,
            base_url=r.base_url,
            timeout_seconds=r.timeout_seconds,
            max_retries=r.max_retries,
        )
    return None


def build_broker(settings: Settings, clock: Clock) -> BrokerReadAdapter | None:
    if settings.broker_adapter == "mock":
        return MockBrokerAdapter(clock=clock)
    return None


def build_sharekhan(
    state: AppState, transport: httpx.BaseTransport | None = None
) -> broker_link.SharekhanLink:
    """Read-only Sharekhan link. Settings already refused missing keys or disabled sign-in."""
    sk = state.settings.sharekhan
    auth_key = state.settings.auth.secret_key
    if sk.api_key is None or sk.secret_key is None or auth_key is None:  # Settings refuses this
        raise RuntimeError("Sharekhan keys and RHYTHM_AUTH__SECRET_KEY are required")
    sealer = broker_link.box(auth_key.get_secret_value())
    client = SharekhanReadClient(
        api_key=sk.api_key.get_secret_value(),
        secret_key=sk.secret_key.get_secret_value(),
        base_url=sk.base_url,
        vendor_key=sk.vendor_key.get_secret_value() if sk.vendor_key else None,
        version_id=sk.version_id,
        timeout_seconds=sk.timeout_seconds,
        transport=transport,
    )

    def session_provider() -> ActiveSession | None:
        with session_scope(state.session_factory) as s:
            return broker_link.active_session(s, sealer, state.clock.now())

    return broker_link.SharekhanLink(
        client=client,
        sealer=sealer,
        broker=SharekhanBroker(client, session_provider, state.clock),
        api_key=sk.api_key.get_secret_value(),
        stream_url=sk.stream_url,
        dashboard_url=sk.dashboard_url,
    )


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    clock: Clock | None = None,
    market_data: MarketDataProvider | None = None,
    broker: BrokerReadAdapter | None = None,
    calendar: SessionCalendar | None = None,
    events: EventProvider | None = None,
    fundamentals: FundamentalsProvider | None = None,
    research_provider: ResearchProvider | None = None,
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
    audit = AuditLog(environment=settings.environment.value, clock=clock)
    rprov = research_provider or build_research_provider(settings)
    state = AppState(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        clock=clock,
        audit=audit,
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
        fundamentals=fundamentals or build_fundamentals(settings, market_data),
        research=None
        if rprov is None or not settings.features.research_service
        else ResearchService(rprov, settings.research, clock, audit),
        auth=AuthService(settings.auth, audit) if settings.auth.enabled else None,
    )
    if settings.broker_adapter == "sharekhan" and broker is None:
        state.sharekhan = build_sharekhan(state)
        state.broker = state.sharekhan.broker
    notifications.configure(settings.notifications)
    wh = settings.notifications.webhook_url
    dispatcher = (
        WebhookDispatcher(
            settings.notifications, state.session_factory, clock, settings.environment.value
        )
        if wh is not None and wh.get_secret_value()
        else None
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
        if dispatcher is not None:
            dispatcher.start()
        yield
        if dispatcher is not None:
            dispatcher.stop()
        _audit_lifecycle(state, "system.stopped")
        log.info("shutdown")

    app = FastAPI(
        title="Rhythm AI Trader API",
        version=__version__,
        description="Research and paper-trading API. Live trading is not available.",
        lifespan=lifespan,
        # Interactive docs only in local/test; hosted environments do not publish the schema.
        docs_url="/docs" if settings.environment.value in ("local", "test") else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.environment.value in ("local", "test") else None,
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
        route = request.scope.get("route")
        metrics.REGISTRY.observe_request(
            request.method,
            str(getattr(route, "path", "unmatched")),
            response.status_code,
            time.perf_counter() - started,
        )
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

    guarded = [Depends(access)]  # authentication + authorisation on every route (Phase 8)
    for r in (
        system.router,
        auth_routes.router,
        instruments.router,
        watchlists.router,
        marketdata.router,
        trading.router,
        risk.router,
        swing.router,
        research.router,
        ops.router,
        scheduler_routes.router,
        broker_routes.router,
    ):
        app.include_router(r, dependencies=guarded)
    return app
