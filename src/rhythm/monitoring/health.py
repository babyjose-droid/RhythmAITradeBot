"""Component health, evaluated from the database, with alarms on every state change.

Statuses: ok | degraded | down. A transition is stored in ``system_health_events``
(component ``monitor.<name>``) and raises a notification: down → critical,
degraded → warning, back to ok → info. Steady states raise nothing (no alarm spam).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rhythm.config.settings import Settings
from rhythm.controls import service as controls
from rhythm.db.models import (
    NotificationRow,
    PaperAccount,
    ReconciliationRow,
    ScheduledJobRow,
    SchedulerHeartbeatRow,
    SystemHealthEvent,
)
from rhythm.notifications.service import notify

SEVERITY = {"down": "critical", "degraded": "warning", "ok": "info"}
WEBHOOK_STUCK = timedelta(minutes=10)


@dataclass(frozen=True)
class ComponentStatus:
    component: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"component": self.component, "status": self.status, "detail": self.detail}


def leader_heartbeat_age(session: Session, now: datetime) -> float | None:
    beat = session.execute(
        select(func.max(SchedulerHeartbeatRow.last_beat_at)).where(SchedulerHeartbeatRow.is_leader)
    ).scalar_one_or_none()
    return None if beat is None else (now - beat).total_seconds()


def evaluate(session: Session, settings: Settings, now: datetime) -> list[ComponentStatus]:
    out: list[ComponentStatus] = []
    ctl = controls.control_state(session)
    out.append(
        ComponentStatus(
            "kill_switch",
            "degraded" if ctl.kill_switch_active else "ok",
            {"active": ctl.kill_switch_active, "paused": sorted(ctl.paused_strategies)},
        )
    )
    breaks: list[int] = []
    for (acct_id,) in session.execute(select(PaperAccount.id)).all():
        last = session.execute(
            select(ReconciliationRow.status)
            .where(ReconciliationRow.account_id == acct_id)
            .order_by(ReconciliationRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last == "BREAK":
            breaks.append(acct_id)
    out.append(ComponentStatus("reconciliation", "down" if breaks else "ok", {"breaks": breaks}))
    age = leader_heartbeat_age(session, now)
    stale = settings.scheduler.heartbeat_stale_seconds
    if settings.scheduler.expected:
        st = "ok" if age is not None and age <= stale else "down"
    else:
        st = "ok"
    out.append(
        ComponentStatus(
            "scheduler",
            st,
            {
                "expected": settings.scheduler.expected,
                "heartbeat_age_seconds": age,
                "stale_after": stale,
            },
        )
    )
    jobs = list(session.execute(select(ScheduledJobRow)).scalars())
    failing = [j.name for j in jobs if j.enabled and j.consecutive_failures > 0]
    auto_off = [
        j.name for j in jobs if not j.enabled and (j.disabled_reason or "").startswith("auto")
    ]
    out.append(
        ComponentStatus(
            "jobs",
            "down" if auto_off else "degraded" if failing else "ok",
            {"failing": failing, "auto_disabled": auto_off},
        )
    )
    failed = session.execute(
        select(func.count())
        .select_from(NotificationRow)
        .where(
            NotificationRow.webhook_status == "FAILED",
            NotificationRow.created_at >= now - timedelta(days=1),
        )
    ).scalar_one()
    stuck = session.execute(
        select(func.count())
        .select_from(NotificationRow)
        .where(
            NotificationRow.webhook_status == "PENDING",
            NotificationRow.created_at < now - WEBHOOK_STUCK,
        )
    ).scalar_one()
    out.append(
        ComponentStatus(
            "notifications_webhook",
            "degraded" if failed or stuck else "ok",
            {"failed_24h": int(failed), "pending_over_10m": int(stuck)},
        )
    )
    return out


def _last_status(session: Session, component: str) -> str | None:
    return session.execute(
        select(SystemHealthEvent.status)
        .where(SystemHealthEvent.component == f"monitor.{component}")
        .order_by(SystemHealthEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def record_transitions(
    session: Session, statuses: list[ComponentStatus], now: datetime
) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []
    for s in statuses:
        prev = _last_status(session, s.component)
        if prev == s.status or (prev is None and s.status == "ok"):
            continue
        session.add(
            SystemHealthEvent(
                occurred_at=now,
                component=f"monitor.{s.component}",
                status=s.status,
                detail=s.detail | {"previous": prev},
            )
        )
        title = (
            f"{s.component.replace('_', ' ')} recovered"
            if s.status == "ok"
            else f"{s.component.replace('_', ' ')} is {s.status.upper()}"
        )
        notify(
            session,
            kind=f"health_{s.component}",
            severity=SEVERITY[s.status],
            title=title,
            body=", ".join(f"{k}: {v}" for k, v in s.detail.items())[:500],
            at=now,
            link="/automation",
            subject_type="component",
            subject_id=s.component,
        )
        changed.append({"component": s.component, "from": prev, "to": s.status})
    session.flush()
    return changed
