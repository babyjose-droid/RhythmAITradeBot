"""Research records: prompt versions, frozen evidence packets, AI assessments (append-only)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
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
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base

ASSESSMENT_STATUSES = (
    "VALID",
    "INVALID",
    "TIMEOUT",
    "ERROR",
    "BUDGET_EXCEEDED",
    "CYCLE_LIMIT",
)


class PromptVersionRow(Base):
    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    system_text: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ResearchPacketRow(Base):
    __tablename__ = "research_packets"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    packet: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    data_is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AIAssessmentRow(Base):
    __tablename__ = "ai_assessments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('VALID','INVALID','TIMEOUT','ERROR','BUDGET_EXCEEDED','CYCLE_LIMIT')",
            name="status",
        ),
        CheckConstraint("context IN ('PAPER', 'RESEARCH')", name="context"),
        CheckConstraint(
            "suggested_action IS NULL OR suggested_action IN "
            "('CONFIRM','HOLD','NO_ACTION','REQUIRES_REVIEW')",
            name="suggested_action",
        ),
        Index("ix_ai_assessments_created", "created_at"),
        Index("ix_ai_assessments_symbol", "exchange", "symbol", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    context: Mapped[str] = mapped_column(String(10), nullable=False)
    packet_id: Mapped[int] = mapped_column(ForeignKey("research_packets.id"), nullable=False)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_signals.id"))
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    prompt_version: Mapped[str] = mapped_column(ForeignKey("prompt_versions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    suggested_action: Mapped[str | None] = mapped_column(String(16))
    gate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    errors: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    raw_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
