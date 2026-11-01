"""Trade theses for multi-day (swing) PAPER positions and their append-only history."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base


class ThesisRow(Base):
    __tablename__ = "theses"
    __table_args__ = (
        CheckConstraint("account_kind = 'PAPER'", name="paper_only"),
        CheckConstraint("status IN ('PENDING_ENTRY','OPEN','INVALIDATED','CLOSED')", name="status"),
        Index("ix_theses_account_status", "account_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    account_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    invalidation: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    ref_price: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    stop: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    target: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(200))
    data_is_mock: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ThesisEventRow(Base):
    """Append-only: every status change and invalidation with its evidence."""

    __tablename__ = "thesis_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    thesis_id: Mapped[int] = mapped_column(ForeignKey("theses.id"), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
