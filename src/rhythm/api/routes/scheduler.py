"""Scheduled jobs (Phase 9): create, enable/disable, run now, run history, heartbeats."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from rhythm.api.auth import actor
from rhythm.api.deps import SessionDep, StateDep
from rhythm.api.idempotency import IdempotencyKey, run_idempotent
from rhythm.db.models import JobRunRow, ScheduledJobRow, SchedulerHeartbeatRow
from rhythm.monitoring import health
from rhythm.scheduler import jobs, slots
from rhythm.scheduler.service import Scheduler, job_dict, run_dict

router = APIRouter(tags=["scheduler"])


def _sched(state: StateDep) -> Scheduler:
    return Scheduler(state, instance="api")


@router.get("/v1/scheduler/status")
def status(state: StateDep, session: SessionDep) -> dict[str, Any]:
    now = state.clock.now()
    beats = session.execute(
        select(SchedulerHeartbeatRow).order_by(SchedulerHeartbeatRow.last_beat_at.desc())
    ).scalars()
    return {
        "expected": state.settings.scheduler.expected,
        "tick_seconds": state.settings.scheduler.tick_seconds,
        "leader_heartbeat_age_seconds": health.leader_heartbeat_age(session, now),
        "stale_after_seconds": state.settings.scheduler.heartbeat_stale_seconds,
        "instances": [
            {
                "instance": b.instance,
                "is_leader": b.is_leader,
                "started_at": b.started_at,
                "last_beat_at": b.last_beat_at,
                "age_seconds": round((now - b.last_beat_at).total_seconds(), 1),
            }
            for b in beats
        ][:10],
        "health": [c.as_dict() for c in health.evaluate(session, state.settings, now)],
    }


@router.get("/v1/scheduler/jobs")
def list_jobs(session: SessionDep) -> list[dict[str, Any]]:
    return [
        job_dict(j)
        for j in session.execute(select(ScheduledJobRow).order_by(ScheduledJobRow.id)).scalars()
    ]


class JobIn(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    kind: Literal["paper_cycle", "events_sync", "fundamentals_sync", "health_check", "audit_verify"]
    params: dict[str, Any] = Field(default_factory=dict)
    schedule: Literal["intraday_5m", "after_close", "daily_at", "every_minutes"]
    schedule_arg: str | None = Field(default=None, max_length=16)
    trading_days_only: bool = True


@router.post("/v1/scheduler/jobs", status_code=201)
def create_job(
    body: JobIn, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    """Create a job (disabled). Owner + recent re-authentication."""

    def produce() -> dict[str, Any]:
        try:
            j = _sched(state).create_job(
                session,
                name=body.name,
                kind=body.kind,
                params=body.params,
                schedule=body.schedule,
                schedule_arg=body.schedule_arg,
                trading_days_only=body.trading_days_only,
                actor=actor(state),
            )
        except (jobs.JobConfigError, slots.ScheduleError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(409, f"a job named {body.name!r} already exists") from exc
        return job_dict(j)

    return run_idempotent(
        session,
        key=idem,
        endpoint="POST /v1/scheduler/jobs",
        body=body.model_dump(),
        produce=produce,
        required=True,
        status_code=201,
    )


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


def _toggle(
    job_id: int, enabled: bool, body: ReasonIn, state: StateDep, session: SessionDep
) -> dict[str, Any]:
    try:
        j = _sched(state).set_enabled(
            session, job_id, enabled=enabled, actor=actor(state), reason=body.reason
        )
    except jobs.JobConfigError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 422, str(exc)) from exc
    return job_dict(j)


@router.post("/v1/scheduler/jobs/{job_id}/enable")
def enable(job_id: int, body: ReasonIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    return _toggle(job_id, True, body, state, session)


@router.post("/v1/scheduler/jobs/{job_id}/disable")
def disable(job_id: int, body: ReasonIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    return _toggle(job_id, False, body, state, session)


@router.post("/v1/scheduler/jobs/{job_id}/run")
def run_now(job_id: int, state: StateDep, session: SessionDep, idem: IdempotencyKey = None) -> Any:
    """Run a job once now (manual trigger), through the same path as a scheduled run."""

    def produce() -> dict[str, Any]:
        if session.get(ScheduledJobRow, job_id) is None:
            raise HTTPException(404, f"job {job_id} not found")
        session.commit()  # the run uses its own transactions
        return _sched(state).run_now(job_id, actor(state))

    return run_idempotent(
        session,
        key=idem,
        endpoint=f"POST /v1/scheduler/jobs/{job_id}/run",
        body={"job_id": job_id},
        produce=produce,
        required=True,
    )


@router.get("/v1/scheduler/jobs/{job_id}/runs")
def runs(
    job_id: int, session: SessionDep, limit: int = Query(50, ge=1, le=500)
) -> list[dict[str, Any]]:
    stmt = (
        select(JobRunRow)
        .where(JobRunRow.job_id == job_id)
        .order_by(JobRunRow.id.desc())
        .limit(limit)
    )
    return [run_dict(r) for r in session.execute(stmt).scalars()]
