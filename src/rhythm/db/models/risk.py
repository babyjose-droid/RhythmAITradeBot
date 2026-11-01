"""Risk policies, risk decisions, trading controls, reconciliations and API idempotency.

``risk_checks``, ``control_events`` and ``reconciliations`` are append-only (DB triggers).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base


class RiskPolicyRow(Base):
    __tablename__ = "risk_policies"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RiskCheckRow(Base):
    """One risk-engine decision with every check result and the exact inputs used."""

    __tablename__ = "risk_checks"
    __table_args__ = (
        CheckConstraint("context IN ('PAPER', 'SIMULATION')", name="context"),
        CheckConstraint("outcome IN ('ACCEPT', 'REDUCE', 'REVIEW', 'BLOCK')", name="outcome"),
        Index("ix_risk_checks_account", "account_id", "decided_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    context: Mapped[str] = mapped_column(String(10), nullable=False)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("paper_accounts.id"))
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    intent: Mapped[str] = mapped_column(String(8), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(24), nullable=False)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ControlEventRow(Base):
    """Kill switch and strategy pause history. Current state = latest event per target."""

    __tablename__ = "control_events"
    __table_args__ = (
        CheckConstraint("control IN ('KILL_SWITCH', 'STRATEGY_PAUSE')", name="control"),
        CheckConstraint("action IN ('ACTIVATE', 'RELEASE')", name="action"),
        CheckConstraint("length(reason) > 0", name="reason_required"),
        Index("ix_control_events_target", "control", "target", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    control: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ReconciliationRow(Base):
    __tablename__ = "reconciliations"
    __table_args__ = (
        CheckConstraint("status IN ('OK', 'BREAK')", name="status"),
        Index("ix_reconciliations_account", "account_id", "run_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    trigger: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    diffs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class IdempotencyRecord(Base):
    """Stored response for a client-supplied Idempotency-Key (replayed on retry)."""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
