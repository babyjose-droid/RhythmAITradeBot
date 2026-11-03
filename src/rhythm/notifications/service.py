"""Create, list and acknowledge notifications; deliver them to an optional webhook.

Delivery uses an outbox: rows are written inside the caller's transaction (so a rolled-back
action never notifies) and a background dispatcher posts them afterwards with retries.
Payloads carry no secrets, credentials or account numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rhythm.config.settings import NotificationSettings
from rhythm.db.models import NotificationRow

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
_config: dict[str, NotificationSettings] = {"current": NotificationSettings()}


def configure(settings: NotificationSettings) -> None:
    """Process-wide webhook settings (set once by the app factory; settings are immutable)."""
    _config["current"] = settings


def notify(
    session: Session,
    *,
    kind: str,
    severity: str,
    title: str,
    body: str = "",
    at: datetime,
    settings: NotificationSettings | None = None,
    link: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
) -> NotificationRow:
    if severity not in SEVERITY_RANK:
        raise ValueError(f"unknown severity {severity!r}")
    cfg = settings or _config["current"]
    if cfg.webhook_url is None or not cfg.webhook_url.get_secret_value():
        status = "NOT_CONFIGURED"
    elif SEVERITY_RANK[severity] < SEVERITY_RANK[cfg.webhook_min_severity]:
        status = "SKIPPED"
    else:
        status = "PENDING"
    row = NotificationRow(
        created_at=at,
        kind=kind[:40],
        severity=severity,
        title=title[:200],
        body=body[:2000],
        link=link,
        subject_type=subject_type,
        subject_id=subject_id,
        webhook_status=status,
        webhook_next_at=at if status == "PENDING" else None,
    )
    session.add(row)
    session.flush()
    return row


def as_dict(n: NotificationRow) -> dict[str, Any]:
    return {
        "id": n.id,
        "created_at": n.created_at,
        "kind": n.kind,
        "severity": n.severity,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "subject_type": n.subject_type,
        "subject_id": n.subject_id,
        "read_at": n.read_at,
        "read_by": n.read_by,
        "webhook": {
            "status": n.webhook_status,
            "attempts": n.webhook_attempts,
            "error": n.webhook_error,
        },
    }


def list_notifications(
    session: Session, *, unread_only: bool = False, limit: int = 50, before_id: int | None = None
) -> list[NotificationRow]:
    stmt = select(NotificationRow).order_by(NotificationRow.id.desc()).limit(limit)
    if unread_only:
        stmt = stmt.where(NotificationRow.read_at.is_(None))
    if before_id is not None:
        stmt = stmt.where(NotificationRow.id < before_id)
    return list(session.execute(stmt).scalars())


def unread_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(NotificationRow.severity, func.count())
        .where(NotificationRow.read_at.is_(None))
        .group_by(NotificationRow.severity)
    ).all()
    out = {s: 0 for s in SEVERITY_RANK}
    out.update({s: int(c) for s, c in rows})
    return out


def mark_read(session: Session, ids: list[int] | None, *, by: str, at: datetime) -> int:
    stmt = select(NotificationRow).where(NotificationRow.read_at.is_(None))
    if ids is not None:
        stmt = stmt.where(NotificationRow.id.in_(ids))
    n = 0
    for row in session.execute(stmt).scalars():
        row.read_at, row.read_by = at, by
        n += 1
    return n


def _payload(n: NotificationRow, environment: str) -> dict[str, Any]:
    text = f"[Rhythm AI Trader · PAPER · {environment}] {n.severity.upper()}: {n.title}"
    if n.body:
        text += f"\n{n.body}"
    return {
        "text": text,  # Slack-compatible
        "kind": n.kind,
        "severity": n.severity,
        "title": n.title,
        "body": n.body,
        "created_at": n.created_at.isoformat(),
        "link": n.link,
        "environment": environment,
        "account_kind": "PAPER",
    }


def deliver_pending(
    session: Session,
    settings: NotificationSettings,
    *,
    now: datetime,
    environment: str,
    client: httpx.Client,
    limit: int = 20,
) -> dict[str, int]:
    """Post due PENDING notifications once each; reschedule failures with backoff."""
    url = settings.webhook_url.get_secret_value() if settings.webhook_url else ""
    if not url:
        return {"sent": 0, "failed": 0}
    rows = session.execute(
        select(NotificationRow)
        .where(
            NotificationRow.webhook_status == "PENDING",
            NotificationRow.webhook_next_at <= now,
        )
        .order_by(NotificationRow.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars()
    sent = failed = 0
    for n in rows:
        n.webhook_attempts += 1
        try:
            r = client.post(
                url, json=_payload(n, environment), timeout=settings.webhook_timeout_seconds
            )
            ok, err = 200 <= r.status_code < 300, f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            ok, err = False, type(exc).__name__  # never include the URL (it is a secret)
        if ok:
            n.webhook_status, n.webhook_error, n.webhook_next_at = "SENT", None, None
            sent += 1
            continue
        n.webhook_error = err
        if n.webhook_attempts >= settings.max_attempts:
            n.webhook_status, n.webhook_next_at = "FAILED", None
        else:
            n.webhook_next_at = now + timedelta(seconds=30 * 2 ** (n.webhook_attempts - 1))
        failed += 1
    return {"sent": sent, "failed": failed}
