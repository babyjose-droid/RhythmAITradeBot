"""The scheduler loop: leader election, heartbeats, due slots, one run per slot, failures.

* **One leader.** Instances compete for a PostgreSQL advisory lock held on a dedicated
  connection; only the holder runs jobs. If it dies, the lock is released and another
  instance takes over on its next tick.
* **One run per slot.** ``job_runs`` is unique on (job, slot); a run row is inserted and
  committed *before* the work starts, so a second scheduler can never repeat it.
* **Isolation.** Each job runs in its own transaction; a failure rolls back everything the
  job did, then a FAILED run is recorded separately.
* **No catch-up storms.** Slots missed for longer than ``missed_grace_minutes`` (scheduler
  stopped, machine asleep) are recorded once as MISSED and not run late.
* **Fail closed.** After ``max_consecutive_failures`` a job is disabled automatically and a
  critical notification is raised. Re-enabling is an owner decision.
"""

from __future__ import annotations

import os
import socket
import time
import traceback
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from rhythm.api.state import AppState
from rhythm.audit import ActorType
from rhythm.core.logging import get_logger
from rhythm.core.redaction import redact_text
from rhythm.db.models import JobRunRow, ScheduledJobRow, SchedulerHeartbeatRow
from rhythm.db.session import session_scope
from rhythm.notifications.service import notify
from rhythm.scheduler import jobs, slots
from rhythm.strategies.registry import STRATEGIES

log = get_logger(__name__)
LOCK_ID = 0x52_48_59_54_48_4D  # "RHYTHM"


def default_instance() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"[:80]


def _err(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")[:480]


def job_dict(j: ScheduledJobRow) -> dict[str, Any]:
    return {
        "id": j.id,
        "name": j.name,
        "kind": j.kind,
        "params": j.params,
        "schedule": j.schedule,
        "schedule_arg": j.schedule_arg,
        "trading_days_only": j.trading_days_only,
        "enabled": j.enabled,
        "created_at": j.created_at,
        "created_by": j.created_by,
        "next_run_at": j.next_run_at,
        "last_run_at": j.last_run_at,
        "last_status": j.last_status,
        "last_error": j.last_error,
        "consecutive_failures": j.consecutive_failures,
        "disabled_reason": j.disabled_reason,
    }


def run_dict(r: JobRunRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "job_id": r.job_id,
        "slot": r.slot,
        "trigger": r.trigger,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "status": r.status,
        "duration_ms": r.duration_ms,
        "instance": r.instance,
        "detail": r.detail,
    }


class Scheduler:
    def __init__(self, state: AppState, instance: str | None = None) -> None:
        self.state = state
        self.instance = instance or default_instance()
        self.started_at = state.clock.now()
        self._lock_conn: Connection | None = None
        self.is_leader = False

    # ------------------------------------------------------------------ leadership
    def _try_lead(self) -> bool:
        try:
            if self._lock_conn is None or self._lock_conn.closed:
                self._lock_conn = self.state.engine.connect()
                self.is_leader = False
            if not self.is_leader:
                got = self._lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_ID}
                ).scalar()
                self._lock_conn.commit()
                self.is_leader = bool(got)
            else:
                self._lock_conn.execute(text("SELECT 1"))  # still connected = still holding it
                self._lock_conn.commit()
        except OperationalError:
            self.release()
        return self.is_leader

    def release(self) -> None:
        if self._lock_conn is not None:
            try:
                if self.is_leader and not self._lock_conn.closed:
                    self._lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_ID})
                # Invalidate (not just return to the pool): a pooled connection would keep
                # the lock's database session alive.
                self._lock_conn.invalidate()
                self._lock_conn.close()
            except Exception:
                log.warning("scheduler_lock_close_failed")
        self._lock_conn, self.is_leader = None, False

    def _heartbeat(self, s: Session, now: datetime, detail: dict[str, Any]) -> None:
        hb = s.get(SchedulerHeartbeatRow, self.instance)
        if hb is None:
            hb = SchedulerHeartbeatRow(
                instance=self.instance, started_at=self.started_at, last_beat_at=now
            )
            s.add(hb)
        hb.last_beat_at, hb.is_leader, hb.detail = now, self.is_leader, detail

    # ------------------------------------------------------------------ jobs
    def create_job(
        self,
        s: Session,
        *,
        name: str,
        kind: str,
        params: dict[str, Any],
        schedule: str,
        schedule_arg: str | None,
        trading_days_only: bool,
        actor: str,
    ) -> ScheduledJobRow:
        slots.validate(schedule, schedule_arg)
        jobs.validate_params(s, kind, params)
        if kind == "paper_cycle":
            horizon = STRATEGIES[params["strategy"]].horizon.value
            want = "intraday_5m" if horizon == "INTRADAY" else "after_close"
            if schedule != want:
                raise jobs.JobConfigError(f"{horizon} strategies run on the {want!r} schedule")
        j = ScheduledJobRow(
            name=name.strip()[:80],
            kind=kind,
            params=params,
            schedule=schedule,
            schedule_arg=schedule_arg,
            trading_days_only=trading_days_only,
            enabled=False,
            created_by=actor,
        )
        s.add(j)
        s.flush()
        self.state.audit.append(
            s,
            event_type="scheduler.job_created",
            actor_type=ActorType.USER,
            actor_id=actor,
            subject_type="scheduled_job",
            subject_id=str(j.id),
            payload={
                "name": j.name,
                "kind": kind,
                "params": params,
                "schedule": schedule,
                "arg": schedule_arg,
            },
        )
        return j

    def set_enabled(
        self, s: Session, job_id: int, *, enabled: bool, actor: str, reason: str
    ) -> ScheduledJobRow:
        j = s.get(ScheduledJobRow, job_id, with_for_update=True)
        if j is None:
            raise jobs.JobConfigError(f"job {job_id} not found")
        now = self.state.clock.now()
        if enabled:
            jobs.validate_params(s, j.kind, j.params)
            j.next_run_at = slots.next_slot(
                j.schedule,
                j.schedule_arg,
                now,
                self.state.calendar,
                trading_days_only=j.trading_days_only,
            )
            j.consecutive_failures, j.disabled_reason = 0, None
        else:
            j.next_run_at, j.disabled_reason = None, reason[:300]
        j.enabled = enabled
        self.state.audit.append(
            s,
            event_type="scheduler.job_enabled" if enabled else "scheduler.job_disabled",
            actor_type=ActorType.USER,
            actor_id=actor,
            subject_type="scheduled_job",
            subject_id=str(j.id),
            payload={"name": j.name, "reason": reason[:300]},
        )
        return j

    def _claim(self, job_id: int, slot: datetime, trigger: str, now: datetime) -> int | None:
        """Insert-and-commit the run row; None if this slot already has a run."""
        try:
            with session_scope(self.state.session_factory) as s:
                run = JobRunRow(
                    job_id=job_id,
                    slot=slot,
                    trigger=trigger,
                    started_at=now,
                    status="RUNNING",
                    instance=self.instance,
                )
                s.add(run)
                s.flush()
                return run.id
        except IntegrityError:
            return None

    def execute(self, job_id: int, slot: datetime, trigger: str) -> dict[str, Any]:
        now = self.state.clock.now()
        run_id = self._claim(job_id, slot, trigger, now)
        if run_id is None:
            return {"job_id": job_id, "status": "DUPLICATE_SLOT"}
        started = time.perf_counter()
        status, detail = "SUCCESS", {}
        try:
            with session_scope(self.state.session_factory) as s:
                j = s.get(ScheduledJobRow, job_id)
                if j is None:
                    raise jobs.JobConfigError(f"job {job_id} disappeared")
                detail = jobs.run(s, self.state, j.kind, j.params, now)
        except Exception as exc:
            status = "FAILED"
            detail = {
                "error": _err(exc),
                "trace": redact_text("".join(traceback.format_exception(exc))[-1500:]),
            }
            log.warning("scheduled_job_failed", job_id=job_id, error=type(exc).__name__)
        self._finish(
            job_id, run_id, slot, status, detail, int((time.perf_counter() - started) * 1000)
        )
        return {"job_id": job_id, "run_id": run_id, "status": status, "detail": detail}

    def _finish(
        self, job_id: int, run_id: int, slot: datetime, status: str, detail: dict[str, Any], ms: int
    ) -> None:
        now = self.state.clock.now()
        max_fail = self.state.settings.scheduler.max_consecutive_failures
        with session_scope(self.state.session_factory) as s:
            run = s.get(JobRunRow, run_id)
            if run is not None:
                run.status, run.finished_at, run.duration_ms, run.detail = status, now, ms, detail
            j = s.get(ScheduledJobRow, job_id, with_for_update=True)
            if j is None:
                return
            j.last_run_at, j.last_status = now, status
            if status == "SUCCESS":
                j.consecutive_failures, j.last_error = 0, None
                return
            j.consecutive_failures += 1
            j.last_error = str(detail.get("error", ""))[:500]
            auto_off = j.enabled and j.consecutive_failures >= max_fail
            notify(
                s,
                kind="job_failed" if not auto_off else "job_auto_disabled",
                severity="critical" if auto_off else "warning",
                title=f"Scheduled job '{j.name}' failed"
                + (f" {j.consecutive_failures} times — disabled" if auto_off else ""),
                body=j.last_error,
                at=now,
                link="/automation",
                subject_type="scheduled_job",
                subject_id=str(j.id),
            )
            if auto_off:
                j.enabled, j.next_run_at = False, None
                j.disabled_reason = (
                    f"auto-disabled after {j.consecutive_failures} consecutive failures"
                )
                self.state.audit.append(
                    s,
                    event_type="scheduler.job_auto_disabled",
                    actor_type=ActorType.SYSTEM,
                    actor_id="scheduler",
                    subject_type="scheduled_job",
                    subject_id=str(j.id),
                    payload={
                        "name": j.name,
                        "failures": j.consecutive_failures,
                        "error": j.last_error,
                    },
                )

    def run_now(self, job_id: int, actor: str) -> dict[str, Any]:
        now = self.state.clock.now()
        with session_scope(self.state.session_factory) as s:
            j = s.get(ScheduledJobRow, job_id)
            if j is None:
                raise jobs.JobConfigError(f"job {job_id} not found")
            self.state.audit.append(
                s,
                event_type="scheduler.job_run_requested",
                actor_type=ActorType.USER,
                actor_id=actor,
                subject_type="scheduled_job",
                subject_id=str(j.id),
                payload={"name": j.name},
            )
        return self.execute(job_id, now, "manual")

    # ------------------------------------------------------------------ loop
    def tick(self) -> dict[str, Any]:
        now = self.state.clock.now()
        leader = self._try_lead()
        ran: list[dict[str, Any]] = []
        due: list[tuple[int, datetime]] = []
        grace = timedelta(minutes=self.state.settings.scheduler.missed_grace_minutes)
        with session_scope(self.state.session_factory) as s:
            self._heartbeat(s, now, {"leader": leader})
            if leader:
                for j in s.execute(
                    select(ScheduledJobRow)
                    .where(ScheduledJobRow.enabled, ScheduledJobRow.next_run_at <= now)
                    .order_by(ScheduledJobRow.next_run_at, ScheduledJobRow.id)
                    .with_for_update(skip_locked=True)
                ).scalars():
                    slot = j.next_run_at
                    assert slot is not None  # noqa: S101 — filtered above
                    nxt = slots.next_slot(
                        j.schedule,
                        j.schedule_arg,
                        now,
                        self.state.calendar,
                        trading_days_only=j.trading_days_only,
                    )
                    if now - slot > grace:
                        missed = [
                            slot,
                            *slots.slots_between(
                                j.schedule,
                                j.schedule_arg,
                                slot,
                                now,
                                self.state.calendar,
                                trading_days_only=j.trading_days_only,
                            ),
                        ]
                        s.add(
                            JobRunRow(
                                job_id=j.id,
                                slot=slot,
                                trigger="schedule",
                                started_at=now,
                                finished_at=now,
                                status="MISSED",
                                duration_ms=0,
                                instance=self.instance,
                                detail={
                                    "missed_slots": len(missed),
                                    "last_missed": missed[-1].isoformat(),
                                    "reason": "scheduler was not running; not run late",
                                },
                            )
                        )
                        j.last_status = "MISSED"
                        ran.append(
                            {"job_id": j.id, "status": "MISSED", "missed_slots": len(missed)}
                        )
                    else:
                        due.append((j.id, slot))
                    j.next_run_at = nxt
        for job_id, slot in due:
            ran.append(self.execute(job_id, slot, "schedule"))
        return {"leader": leader, "at": now.isoformat(), "runs": ran}

    def run_forever(self) -> None:  # pragma: no cover — exercised by the container
        tick_s = self.state.settings.scheduler.tick_seconds
        log.info("scheduler_started", instance=self.instance)
        try:
            while True:
                try:
                    res = self.tick()
                    if res["runs"]:
                        log.info("scheduler_tick", leader=res["leader"], runs=len(res["runs"]))
                except Exception:
                    log.exception("scheduler_tick_failed")
                    self.release()
                time.sleep(tick_s)
        finally:
            self.release()
