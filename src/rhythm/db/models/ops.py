"""Phase 8: users, sessions, approvals and notifications."""

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
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base

ROLES = ("owner", "approver", "viewer")


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('owner', 'approver', 'viewer')", name="role"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    totp_secret_sealed: Mapped[str] = mapped_column(Text, nullable=False)
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user", "user_id"),)

    # SHA-256 of the cookie token: a database leak does not reveal live session tokens.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reauth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class ApprovalRow(Base):
    """A REQUIRES_REVIEW candidate waiting for a human decision (PAPER)."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','EXPIRED','FAILED')", name="status"
        ),
        Index("ix_approvals_status", "status", "created_at"),
        Index("ix_approvals_account", "account_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), nullable=False)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_signals.id"))
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # RISK | RESEARCH
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    data_is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    decision_ip: Mapped[str | None] = mapped_column(String(64))
    decision_risk: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))


class NotificationRow(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("severity IN ('info','warning','critical')", name="severity"),
        CheckConstraint(
            "webhook_status IN ('NOT_CONFIGURED','SKIPPED','PENDING','SENT','FAILED')",
            name="webhook_status",
        ),
        Index("ix_notifications_created", "created_at"),
        Index("ix_notifications_webhook", "webhook_status", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    link: Mapped[str | None] = mapped_column(String(200))
    subject_type: Mapped[str | None] = mapped_column(String(40))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_by: Mapped[str | None] = mapped_column(String(64))
    webhook_status: Mapped[str] = mapped_column(String(16), nullable=False)
    webhook_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    webhook_error: Mapped[str | None] = mapped_column(String(300))
    webhook_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
