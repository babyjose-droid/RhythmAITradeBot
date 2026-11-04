"""A small Prometheus text-format registry (no extra dependency).

Request counters/latency are recorded in-process by the API middleware; business gauges
(kill switch, approvals, webhook backlog, scheduler heartbeat, job failures, reconciliation)
are read from the database at scrape time so they are correct across processes.
"""

from __future__ import annotations

import threading
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rhythm.controls import service as controls
from rhythm.db.models import (
    ApprovalRow,
    AuditEvent,
    NotificationRow,
    PaperAccount,
    ReconciliationRow,
    ScheduledJobRow,
)
from rhythm.monitoring.health import leader_heartbeat_age

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(d: dict[str, str]) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in sorted(d.items())) + "}" if d else ""


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self.hist: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * (len(BUCKETS) + 1))
        self.sums: dict[tuple[str, str], float] = defaultdict(float)

    def observe_request(self, method: str, route: str, status: int, seconds: float) -> None:
        with self._lock:
            self.requests[(method, route, str(status))] += 1
            self.hist[(method, route)][bisect_left(BUCKETS, seconds)] += 1
            self.sums[(method, route)] += seconds

    def render_requests(self) -> list[str]:
        req = "rhythm_http_requests_total"
        dur = "rhythm_http_request_duration_seconds"
        out = [
            f"# HELP {req} HTTP requests by route template and status.",
            f"# TYPE {req} counter",
        ]
        with self._lock:
            for (m, r, s), n in sorted(self.requests.items()):
                out.append(f"{req}{_labels({'method': m, 'route': r, 'status': s})} {n}")
            out += [f"# HELP {dur} Request latency.", f"# TYPE {dur} histogram"]
            for (m, r), counts in sorted(self.hist.items()):
                base = {"method": m, "route": r}
                cum = 0
                for i, b in enumerate(BUCKETS):
                    cum += counts[i]
                    out.append(f"{dur}_bucket{_labels(base | {'le': str(b)})} {cum}")
                cum += counts[-1]
                out.append(f"{dur}_bucket{_labels(base | {'le': '+Inf'})} {cum}")
                out.append(f"{dur}_count{_labels(base)} {cum}")
                out.append(f"{dur}_sum{_labels(base)} {self.sums[(m, r)]:.6f}")
        return out


REGISTRY = Registry()


def _gauge(name: str, help_: str, values: list[tuple[dict[str, str], float]]) -> list[str]:
    out = [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
    out += [f"{name}{_labels(lbl)} {v:g}" for lbl, v in values]
    return out


def render(session: Session, now: datetime, *, environment: str, trading_mode: str) -> str:
    ctl = controls.control_state(session)
    pending = session.execute(
        select(func.count()).select_from(ApprovalRow).where(ApprovalRow.status == "PENDING")
    ).scalar_one()
    unread = dict(
        session.execute(
            select(NotificationRow.severity, func.count())
            .where(NotificationRow.read_at.is_(None))
            .group_by(NotificationRow.severity)
        ).all()
    )
    webhook = dict(
        session.execute(
            select(NotificationRow.webhook_status, func.count())
            .where(NotificationRow.webhook_status.in_(("PENDING", "FAILED")))
            .group_by(NotificationRow.webhook_status)
        ).all()
    )
    age = leader_heartbeat_age(session, now)
    jobs = list(session.execute(select(ScheduledJobRow)).scalars())
    recon: list[tuple[dict[str, str], float]] = []
    for (acct_id,) in session.execute(select(PaperAccount.id)).all():
        last = session.execute(
            select(ReconciliationRow.status)
            .where(ReconciliationRow.account_id == acct_id)
            .order_by(ReconciliationRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last is not None:
            recon.append(({"account_id": str(acct_id)}, 1.0 if last == "OK" else 0.0))
    audit_rows = session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
    lines = [
        *_gauge(
            "rhythm_up",
            "1 when the API can reach its database.",
            [({"environment": environment, "mode": trading_mode}, 1)],
        ),
        *_gauge(
            "rhythm_kill_switch_active",
            "Global PAPER kill switch.",
            [({}, 1.0 if ctl.kill_switch_active else 0.0)],
        ),
        *_gauge(
            "rhythm_strategies_paused",
            "Paused strategies.",
            [({}, float(len(ctl.paused_strategies)))],
        ),
        *_gauge(
            "rhythm_approvals_pending",
            "Approval requests waiting for a decision.",
            [({}, float(pending))],
        ),
        *_gauge(
            "rhythm_notifications_unread",
            "Unread notifications by severity.",
            [({"severity": s}, float(unread.get(s, 0))) for s in ("info", "warning", "critical")],
        ),
        *_gauge(
            "rhythm_webhook_backlog",
            "Notifications not delivered to the webhook.",
            [({"status": s}, float(webhook.get(s, 0))) for s in ("PENDING", "FAILED")],
        ),
        *_gauge(
            "rhythm_scheduler_heartbeat_age_seconds",
            "Seconds since the leader scheduler's last heartbeat (-1 = never).",
            [({}, -1.0 if age is None else age)],
        ),
        *_gauge(
            "rhythm_job_consecutive_failures",
            "Consecutive failures per scheduled job.",
            [
                ({"job": j.name, "enabled": str(j.enabled).lower()}, float(j.consecutive_failures))
                for j in jobs
            ],
        ),
        *_gauge(
            "rhythm_reconciliation_ok", "1 when the account's last reconciliation was OK.", recon
        ),
        *_gauge(
            "rhythm_audit_events", "Rows in the append-only audit log.", [({}, float(audit_rows))]
        ),
        *REGISTRY.render_requests(),
    ]
    return "\n".join(lines) + "\n"
