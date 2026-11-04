"""Read-only Sharekhan account link (/v1/broker/*).

Connect: an owner (recently re-authenticated) asks for a login URL and signs in on
Sharekhan's own page. Sharekhan redirects the browser to ``/v1/broker/callback``, which is
public because the redirect is cross-site (our SameSite=Strict cookie is not sent); it is
protected instead by the single-use ``state`` minted for that owner.

Every endpoint here reads. There is no route, and no adapter method, that places, modifies
or cancels an order.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from rhythm.api.auth import actor
from rhythm.api.deps import SessionDep, StateDep
from rhythm.audit import ActorType
from rhythm.broker.base import BrokerAuthError, BrokerError, BrokerRateLimitedError
from rhythm.broker.sharekhan.adapter import probe_stream
from rhythm.brokerlink import service as link
from rhythm.core.clock import to_exchange_local
from rhythm.core.logging import get_logger
from rhythm.core.market import DataOrigin, Exchange
from rhythm.db.models import Instrument
from rhythm.db.session import session_scope

router = APIRouter(prefix="/v1/broker", tags=["broker (read-only)"])
log = get_logger(__name__)
FEED_KEY = re.compile(r"^(NC|BC)\d{1,10}$")


def _link(state: StateDep) -> link.SharekhanLink:
    sk = state.sharekhan
    if sk is None:
        raise HTTPException(
            409, "Sharekhan is not configured: set RHYTHM_BROKER_ADAPTER=sharekhan (see README)"
        )
    return sk


def _read(fn: Any) -> Any:
    try:
        return fn()
    except BrokerAuthError as exc:
        raise HTTPException(401, f"Sharekhan login required: {exc}") from exc
    except BrokerRateLimitedError as exc:
        raise HTTPException(429, str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc


def _rows(items: Any) -> list[dict[str, Any]]:
    out = []
    for it in items:
        d = asdict(it)
        for k, v in list(d.items()):
            if hasattr(v, "value"):
                d[k] = v.value
        out.append(d)
    return out


@router.get("/status")
def broker_status(state: StateDep, session: SessionDep) -> dict[str, Any]:
    now = state.clock.now()
    configured = state.sharekhan is not None
    out: dict[str, Any] = {
        "adapter": state.settings.broker_adapter,
        "configured": configured,
        "read_only": True,
        "order_placement": "not implemented",
        "market_open": state.calendar.is_session_open(now),
    }
    if configured:
        out |= link.status(session, now)
        out["last_verification"] = link.latest_verification(session)
    return out


@router.post("/login")
def broker_login(state: StateDep, session: SessionDep) -> dict[str, Any]:
    """Owner + recent re-auth. Returns the Sharekhan login URL (valid 10 minutes, single use)."""
    sk = _link(state)
    url = link.start_login(session, sk.client, actor=actor(state), now=state.clock.now())
    return {"login_url": url, "expires_in_seconds": int(link.STATE_TTL.total_seconds())}


@router.get("/callback", include_in_schema=False)
def broker_callback(
    state: StateDep,
    request_token: str | None = Query(default=None, max_length=4096),
    requestToken: str | None = Query(default=None, max_length=4096),  # noqa: N803
    login_state: str | None = Query(default=None, alias="state", max_length=128),
) -> RedirectResponse:
    sk = _link(state)
    token = request_token or requestToken
    result: dict[str, str]
    if not token or not login_state:
        result = {"error": "missing request_token or state"}
    else:
        try:
            with session_scope(state.session_factory) as s:
                link.complete_login(
                    s,
                    sk.client,
                    sk.sealer,
                    state.audit,
                    request_token=token,
                    state=login_state,
                    now=state.clock.now(),
                )
            result = {"connected": "1"}
        except link.LoginStateError as exc:
            result = {"error": str(exc)}
        except BrokerError as exc:
            log.warning("broker_login_failed", error_type=type(exc).__name__)
            result = {"error": f"Sharekhan sign-in failed ({type(exc).__name__}): {exc}"[:200]}
    return RedirectResponse(f"{sk.dashboard_url}?{urlencode(result)}", status_code=302)


@router.post("/logout")
def broker_logout(state: StateDep, session: SessionDep) -> dict[str, Any]:
    _link(state)
    n = link.logout(session, state.audit, actor=actor(state), now=state.clock.now())
    return {"revoked": n}


@router.get("/profile")
def profile(state: StateDep) -> dict[str, Any]:
    sk = _link(state)
    p = _read(sk.broker.get_profile)
    return {**asdict(p), "origin": p.origin.value}


@router.get("/funds")
def funds(state: StateDep, exchange: Exchange = Exchange.NSE) -> dict[str, Any]:
    sk = _link(state)
    f = _read(lambda: sk.broker.get_funds(exchange))
    return {
        "exchange": f.exchange.value,
        "cash_balance": f.cash_balance,
        "reported_fields": dict(f.raw_fields),
        "origin": f.origin.value,
        "as_of": f.as_of,
        "note": "Available limit is not computed: Sharekhan's formula is undocumented (B-05).",
    }


@router.get("/holdings")
def holdings(state: StateDep) -> list[dict[str, Any]]:
    sk = _link(state)
    return _rows(_read(sk.broker.get_holdings))


@router.get("/positions")
def positions(state: StateDep) -> list[dict[str, Any]]:
    sk = _link(state)
    return _rows(_read(sk.broker.get_positions))


@router.get("/orders")
def day_orders(state: StateDep) -> list[dict[str, Any]]:
    """Today's orders as reported by Sharekhan (read only)."""
    sk = _link(state)
    return _rows(_read(sk.broker.get_day_orders))


def _scrip_code(session: Any, exchange: Exchange, symbol: str) -> str:
    code = session.execute(
        select(Instrument.source_code).where(
            Instrument.exchange == exchange.value,
            Instrument.symbol == symbol.upper(),
            Instrument.source == DataOrigin.SHAREKHAN.value,
            Instrument.valid_to.is_(None),
        )
    ).scalar_one_or_none()
    if code is None:
        raise HTTPException(
            404, f"{symbol} has no Sharekhan scrip code: sync the instrument list first"
        )
    return str(code)


@router.get("/historical/{exchange}/{symbol}")
def historical(
    exchange: Exchange,
    symbol: str,
    state: StateDep,
    session: SessionDep,
    interval: str = Query(default="daily", max_length=16),
    limit: int = Query(default=250, ge=1, le=2000),
) -> dict[str, Any]:
    sk = _link(state)
    code = _scrip_code(session, exchange, symbol)
    try:
        candles = _read(lambda: sk.broker.get_candles(exchange, code, interval))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "exchange": exchange.value,
        "symbol": symbol.upper(),
        "interval": interval,
        "origin": DataOrigin.SHAREKHAN.value,
        "adjusted_for_corporate_actions": False,
        "candles": [asdict(c) for c in candles[-limit:]],
    }


class VerifyIn(BaseModel):
    historical_symbol: str | None = Field(default="RELIANCE", max_length=32)


@router.post("/verify")
def verify(body: VerifyIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    """Call each read endpoint once; record field names, types, counts and latency only."""
    sk = _link(state)
    if link.current_row(session, state.clock.now()) is None:
        raise HTTPException(401, "Sharekhan login required")
    scrip: str | None = None
    if body.historical_symbol:
        try:
            scrip = _scrip_code(session, Exchange.NSE, body.historical_symbol)
        except HTTPException:
            scrip = None  # instrument list not synced yet: historical check is skipped
    run_id, results = link.verify(
        session,
        sk.broker,
        state.audit,
        actor=actor(state),
        now=state.clock.now(),
        historical_scrip=scrip,
    )
    return {
        "run_id": run_id,
        "checks": [r.as_dict() for r in results],
        "historical_skipped": scrip is None,
    }


class StreamIn(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["RELIANCE"], min_length=1, max_length=5)
    seconds: float = Field(default=10, ge=1, le=20)


@router.post("/verify/stream")
def verify_stream(body: StreamIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    """Subscribe to last-traded-price for up to 5 instruments for at most 20 s, then disconnect."""
    sk = _link(state)
    now = state.clock.now()
    active = link.active_session(session, sk.sealer, now)
    if active is None:
        raise HTTPException(401, "Sharekhan login required")
    keys: list[str] = []
    for sym in body.symbols:
        key = (
            sym.upper()
            if FEED_KEY.match(sym.upper())
            else f"NC{_scrip_code(session, Exchange.NSE, sym)}"
        )
        keys.append(key)
    probe = probe_stream(sk.stream_url, active.access_token, sk.api_key, keys, body.seconds)
    market_open = state.calendar.is_session_open(now)
    state.audit.append(
        session,
        event_type="broker.verification.stream",
        actor_type=ActorType.USER,
        actor_id=actor(state),
        payload={
            "broker": "sharekhan",
            "connected": probe.connected,
            "messages": probe.messages,
            "error": probe.error,
            "market_open": market_open,
        },
    )
    return {
        **asdict(probe),
        "feed_keys": keys,
        "market_open": market_open,
        "ist_time": to_exchange_local(now).strftime("%H:%M"),
        "note": None if market_open else "Market closed: few or no price messages are expected.",
    }
