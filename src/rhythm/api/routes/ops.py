"""Approvals queue, notifications inbox and the automation overview (Phase 8)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from rhythm.api.auth import actor, client_ip
from rhythm.api.deps import SessionDep, StateDep
from rhythm.api.idempotency import IdempotencyKey, run_idempotent
from rhythm.controls import service as controls
from rhythm.db.models import ApprovalRow, AuditEvent, ScheduledJobRow
from rhythm.monitoring import health
from rhythm.notifications import service as notes
from rhythm.paper import approvals
from rhythm.paper import service as paper
from rhythm.strategies.registry import STRATEGIES

router = APIRouter(tags=["operations"])


# ---------------------------------------------------------------------------- approvals
@router.get("/v1/approvals")
def list_approvals(
    state: StateDep,
    session: SessionDep,
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "FAILED"] | None = None,
    account_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    approvals.expire_due(session, state.clock.now(), state.audit)
    stmt = select(ApprovalRow).order_by(ApprovalRow.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(ApprovalRow.status == status)
    if account_id is not None:
        stmt = stmt.where(ApprovalRow.account_id == account_id)
    return [approvals.as_dict(a) for a in session.execute(stmt).scalars()]


@router.get("/v1/approvals/{approval_id}")
def get_approval(approval_id: int, state: StateDep, session: SessionDep) -> dict[str, Any]:
    approvals.expire_due(session, state.clock.now(), state.audit)
    a = session.get(ApprovalRow, approval_id)
    if a is None:
        raise HTTPException(404, f"approval {approval_id} not found")
    return approvals.as_dict(a)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=500)


@router.post("/v1/approvals/{approval_id}/decision")
def decide(
    approval_id: int,
    body: DecisionIn,
    request: Request,
    state: StateDep,
    session: SessionDep,
    idem: IdempotencyKey = None,
) -> Any:
    """Approve (re-runs every risk check, then a PAPER order) or reject a queued candidate."""

    def produce() -> dict[str, Any]:
        a = session.get(ApprovalRow, approval_id)
        if a is None:
            raise HTTPException(404, f"approval {approval_id} not found")
        cls = STRATEGIES.get(a.strategy)
        try:
            out = paper.decide_approval(
                session,
                approval_id,
                approve=body.decision == "approve",
                reason=body.reason,
                actor_id=actor(state),
                ip=client_ip(request),
                strategy=cls() if cls else None,
                provider=state.market_data,
                settings=state.settings,
                clock=state.clock,
                calendar=state.calendar,
                audit=state.audit,
                policy=state.freshness.policy,
            )
        except approvals.ApprovalStateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except paper.PaperTradingDisabledError as exc:
            raise HTTPException(409, str(exc)) from exc
        return approvals.as_dict(out)

    return run_idempotent(
        session,
        key=idem,
        endpoint=f"POST /v1/approvals/{approval_id}/decision",
        body=body.model_dump(),
        produce=produce,
        required=True,
        headers={"x-account-kind": "PAPER"},
    )


# ---------------------------------------------------------------------------- notifications
@router.get("/v1/notifications")
def list_notifications(
    session: SessionDep,
    unread: bool = False,
    limit: int = Query(50, ge=1, le=500),
    before_id: int | None = Query(None, ge=1),
) -> list[dict[str, Any]]:
    rows = notes.list_notifications(session, unread_only=unread, limit=limit, before_id=before_id)
    return [notes.as_dict(n) for n in rows]


@router.get("/v1/notifications/summary")
def notifications_summary(state: StateDep, session: SessionDep) -> dict[str, Any]:
    counts = notes.unread_counts(session)
    wh = state.settings.notifications.webhook_url
    return {
        "unread": counts,
        "unread_total": sum(counts.values()),
        "webhook_configured": bool(wh and wh.get_secret_value()),
        "webhook_min_severity": state.settings.notifications.webhook_min_severity,
    }


class ReadIn(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=500)  # None = all unread


@router.post("/v1/notifications/read")
def mark_read(body: ReadIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    n = notes.mark_read(session, body.ids, by=actor(state), at=state.clock.now())
    return {"marked_read": n}


# ---------------------------------------------------------------------------- automation
@router.get("/v1/automation")
def automation(state: StateDep, session: SessionDep) -> dict[str, Any]:
    ctl = controls.control_state(session)
    events = session.execute(
        select(AuditEvent)
        .where(AuditEvent.event_type.in_(("paper.cycle", "approval.executed")))
        .order_by(AuditEvent.id.desc())
        .limit(300)
    ).scalars()
    last: dict[str, dict[str, Any]] = {}
    for e in events:
        name = str(e.payload.get("strategy", "")).split("@")[0]
        if name and name not in last:
            last[name] = {
                "at": e.occurred_at,
                "by": e.actor_id,
                "kind": e.event_type,
                "account_id": e.subject_id,
                "new_orders": e.payload.get("new_orders"),
                "new_fills": e.payload.get("new_fills"),
                "signals": e.payload.get("signals"),
                "data_is_mock": e.payload.get("data_is_mock"),
            }
    pending = session.execute(
        select(func.count()).select_from(ApprovalRow).where(ApprovalRow.status == "PENDING")
    ).scalar_one()
    st = state.settings
    wh = st.notifications.webhook_url
    jobs_all = list(session.execute(select(ScheduledJobRow)).scalars())
    return {
        "trading_mode": st.trading_mode.value,
        "kill_switch_active": ctl.kill_switch_active,
        "strategies": [
            {
                "name": name,
                "version": cls.version,
                "horizon": cls.horizon.value,
                "paused": name in ctl.paused_strategies,
                "run_after": "every 5 minutes during the session"
                if cls.horizon.value == "INTRADAY"
                else "once after the close (from ~15:45 IST)",
                "last_run": last.get(name),
            }
            for name, cls in STRATEGIES.items()
        ],
        "approvals": {
            "policy": "REQUIRES_REVIEW candidates only (decision D-06); others go straight to "
            "the PAPER order path after the risk engine",
            "pending": int(pending),
        },
        "research": {
            "provider": st.research_provider,
            "gate_paper_entries": st.research.gate_paper_entries,
        },
        "notifications": {
            "webhook_configured": bool(wh and wh.get_secret_value()),
            "min_severity": st.notifications.webhook_min_severity,
        },
        "scheduler": {
            "configured": bool(jobs_all),
            "expected": st.scheduler.expected,
            "enabled_jobs": sum(1 for j in jobs_all if j.enabled),
            "jobs": len(jobs_all),
            "leader_heartbeat_age_seconds": health.leader_heartbeat_age(session, state.clock.now()),
            "note": "Jobs run in the rhythm-scheduler process (one leader; one run per slot). "
            "Create and enable them under Automation → Scheduled jobs.",
        },
    }
