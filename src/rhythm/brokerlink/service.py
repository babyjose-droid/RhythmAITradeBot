"""Sharekhan read-only link service (database side).

* ``start_login``  - owner asks to connect; we mint a single-use state and return the
  Sharekhan login URL. The owner signs in on Sharekhan's own page; we never see the password.
* ``complete_login`` - Sharekhan redirects back with ``request_token`` + ``state``. The state
  must be one we issued, unused and under 10 minutes old. The access token (24 h) and the
  customer ID are sealed (Fernet, key derived from RHYTHM_AUTH__SECRET_KEY) before storage.
* ``active_session`` - the current session, decrypted in memory for the adapter.
* ``verify`` - calls each read endpoint once and records field names / types / counts only.

Nothing here can place, modify or cancel an order: the adapter it drives has no such method.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.auth.secrets_box import SecretBox
from rhythm.broker.base import BrokerAuthError, BrokerError
from rhythm.broker.sharekhan.adapter import ActiveSession, SharekhanBroker, mask
from rhythm.broker.sharekhan.client import SessionGrant, SharekhanReadClient
from rhythm.broker.sharekhan.readparsers import shape
from rhythm.core.market import Exchange
from rhythm.db.models import BrokerLoginStateRow, BrokerObservationRow, BrokerSessionRow

BROKER = "sharekhan"
STATE_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(hours=24)  # documented: no refresh token
BOX_CONTEXT = "rhythm-broker-session-v1"


def box(auth_secret_key: str) -> SecretBox:
    return SecretBox(auth_secret_key, context=BOX_CONTEXT)


def _h(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def start_login(session: Session, client: SharekhanReadClient, *, actor: str, now: datetime) -> str:
    state = secrets.token_urlsafe(24)
    session.add(
        BrokerLoginStateRow(
            state_hash=_h(state),
            broker=BROKER,
            created_by=actor,
            created_at=now,
            expires_at=now + STATE_TTL,
        )
    )
    return client.login_url(state)


class LoginStateError(BrokerAuthError):
    pass


def complete_login(
    session: Session,
    client: SharekhanReadClient,
    sealer: SecretBox,
    audit: AuditLog,
    *,
    request_token: str,
    state: str,
    now: datetime,
) -> BrokerSessionRow:
    row = session.execute(
        select(BrokerLoginStateRow)
        .where(BrokerLoginStateRow.state_hash == _h(state))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.broker != BROKER:
        raise LoginStateError("unknown login state: start the connection from the dashboard")
    if row.used_at is not None:
        raise LoginStateError("this login link was already used")
    if now > row.expires_at:
        raise LoginStateError("the login link expired (10 minutes): start again")
    row.used_at = now
    session.flush()
    try:
        grant: SessionGrant = client.exchange_request_token(request_token, state)
    except BrokerError as exc:
        audit.append(
            session,
            event_type="broker.session.failed",
            actor_type=ActorType.USER,
            actor_id=row.created_by,
            payload={"broker": BROKER, "error": type(exc).__name__},
        )
        raise
    revoke_all(session, now)
    s = BrokerSessionRow(
        broker=BROKER,
        token_sealed=sealer.seal(grant.access_token),
        customer_sealed=sealer.seal(grant.customer_id),
        account_ref=mask(grant.customer_id),
        exchanges=list(grant.exchanges),
        created_at=now,
        created_by=row.created_by,
        expires_at=now + SESSION_TTL,
    )
    session.add(s)
    session.flush()
    audit.append(
        session,
        event_type="broker.session.created",
        actor_type=ActorType.USER,
        actor_id=row.created_by,
        subject_type="broker_session",
        subject_id=str(s.id),
        payload={
            "broker": BROKER,
            "account_ref": s.account_ref,
            "expires_at": s.expires_at.isoformat(),
        },
    )
    return s


def current_row(session: Session, now: datetime) -> BrokerSessionRow | None:
    return session.execute(
        select(BrokerSessionRow)
        .where(
            BrokerSessionRow.broker == BROKER,
            BrokerSessionRow.revoked_at.is_(None),
            BrokerSessionRow.expires_at > now,
        )
        .order_by(BrokerSessionRow.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def active_session(session: Session, sealer: SecretBox, now: datetime) -> ActiveSession | None:
    row = current_row(session, now)
    if row is None:
        return None
    return ActiveSession(
        access_token=sealer.open(row.token_sealed),
        customer_id=sealer.open(row.customer_sealed),
        account_ref=row.account_ref,
        exchanges=tuple(row.exchanges),
    )


def revoke_all(session: Session, now: datetime) -> int:
    res = session.execute(
        update(BrokerSessionRow)
        .where(BrokerSessionRow.broker == BROKER, BrokerSessionRow.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return int(getattr(res, "rowcount", 0) or 0)


def logout(session: Session, audit: AuditLog, *, actor: str, now: datetime) -> int:
    """Forget the local session. (Sharekhan's token still expires on its own within 24 h.)"""
    n = revoke_all(session, now)
    audit.append(
        session,
        event_type="broker.session.revoked",
        actor_type=ActorType.USER,
        actor_id=actor,
        payload={"broker": BROKER, "sessions": n},
    )
    return n


def status(session: Session, now: datetime) -> dict[str, Any]:
    row = current_row(session, now)
    return {
        "broker": BROKER,
        "read_only": True,
        "connected": row is not None,
        "account_ref": row.account_ref if row else None,
        "exchanges": list(row.exchanges) if row else [],
        "connected_at": row.created_at if row else None,
        "expires_at": row.expires_at if row else None,
        "expires_in_minutes": int((row.expires_at - now).total_seconds() // 60) if row else None,
    }


# --- verification ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    check: str
    endpoint: str
    outcome: str  # OK / EMPTY / ERROR
    http_status: int | None
    latency_ms: int | None
    records: int | None
    detail: str | None
    shape: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def verify(
    session: Session,
    broker: SharekhanBroker,
    audit: AuditLog,
    *,
    actor: str,
    now: datetime,
    historical_scrip: str | None = None,
) -> tuple[str, list[CheckResult]]:
    run_id = uuid.uuid4().hex[:16]
    checks: list[tuple[str, Any]] = [
        ("profile", broker.get_profile),
        ("funds_nse", lambda: broker.get_funds(Exchange.NSE)),
        ("holdings", broker.get_holdings),
        ("positions", broker.get_positions),
        ("day_orders", broker.get_day_orders),
        ("scrip_master_nse", lambda: broker.get_scrip_master(Exchange.NSE)),
    ]
    if historical_scrip:
        checks.append(
            (
                "historical_daily",
                lambda: broker.get_candles(Exchange.NSE, historical_scrip, "daily"),
            )
        )
    results: list[CheckResult] = []
    for name, fn in checks:
        broker.last_raw = None
        records: int | None = None
        detail: str | None = None
        try:
            out = fn()
            records = len(out) if isinstance(out, list | tuple) else 1
            outcome = "OK" if records else "EMPTY"
        except BrokerError as exc:
            outcome, detail = "ERROR", f"{type(exc).__name__}: {exc}"[:300]
        raw = broker.last_raw
        endpoint = raw.path_template if raw else ("(session)" if name == "profile" else "?")
        sh: dict[str, Any] | None = None
        if raw is not None:
            sh = shape(raw.data) if raw.data is not None else None
        res = CheckResult(
            name,
            endpoint,
            outcome,
            raw.http_status if raw else None,
            raw.latency_ms if raw else None,
            records,
            detail,
            sh,
        )
        results.append(res)
        session.add(
            BrokerObservationRow(
                run_id=run_id,
                broker=BROKER,
                check=name,
                endpoint=endpoint,
                http_status=res.http_status,
                latency_ms=res.latency_ms,
                outcome=outcome,
                detail=detail,
                shape=sh,
                observed_at=now,
            )
        )
    audit.append(
        session,
        event_type="broker.verification.run",
        actor_type=ActorType.USER,
        actor_id=actor,
        subject_type="broker_verification",
        subject_id=run_id,
        payload={"broker": BROKER, "outcomes": {r.check: r.outcome for r in results}},
    )
    return run_id, results


def latest_verification(session: Session) -> dict[str, Any] | None:
    last = session.execute(
        select(BrokerObservationRow).order_by(BrokerObservationRow.id.desc()).limit(1)
    ).scalar_one_or_none()
    if last is None:
        return None
    rows = session.execute(
        select(BrokerObservationRow)
        .where(BrokerObservationRow.run_id == last.run_id)
        .order_by(BrokerObservationRow.id)
    ).scalars()
    return {
        "run_id": last.run_id,
        "observed_at": last.observed_at,
        "checks": [
            {
                "check": r.check,
                "endpoint": r.endpoint,
                "outcome": r.outcome,
                "http_status": r.http_status,
                "latency_ms": r.latency_ms,
                "detail": r.detail,
                "shape": r.shape,
            }
            for r in rows
        ],
    }


# --- process wiring -------------------------------------------------------------------------


@dataclass
class SharekhanLink:
    """What the API needs: the read client, the sealer and the adapter (all read-only)."""

    client: SharekhanReadClient
    sealer: SecretBox
    broker: SharekhanBroker
    api_key: str
    stream_url: str
    dashboard_url: str
