"""Phase 9: scheduled jobs, their runs, and scheduler heartbeats."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base

JOB_KINDS = ("paper_cycle", "events_sync", "fundamentals_sync", "health_check", "audit_verify")
SCHEDULES = ("intraday_5m", "after_close", "daily_at", "every_minutes")


class ScheduledJobRow(Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('paper_cycle','events_sync','fundamentals_sync',"
            "'health_check','audit_verify')",
            name="kind",
        ),
        CheckConstraint(
            "schedule IN ('intraday_5m','after_close','daily_at','every_minutes')", name="schedule"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schedule: Mapped[str] = mapped_column(String(16), nullable=False)
    # daily_at: "HH:MM" IST; every_minutes: "N".
    schedule_arg: Mapped[str | None] = mapped_column(String(16))
    trading_days_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(String(500))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disabled_reason: Mapped[str | None] = mapped_column(String(300))


class JobRunRow(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        # One run per job per scheduled slot: a second scheduler can never run it twice.
        UniqueConstraint("job_id", "slot", name="uq_job_runs_slot"),
        CheckConstraint(
            "status IN ('RUNNING','SUCCESS','FAILED','SKIPPED','MISSED')", name="status"
        ),
        Index("ix_job_runs_job", "job_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scheduled_jobs.id"), nullable=False)
    slot: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # schedule | manual
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    instance: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class SchedulerHeartbeatRow(Base):
    __tablename__ = "scheduler_heartbeats"

    instance: Mapped[str] = mapped_column(String(80), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_leader: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
