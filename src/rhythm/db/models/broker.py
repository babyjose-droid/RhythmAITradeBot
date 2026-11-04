"""Sharekhan read-only link: sessions, single-use login states, response-shape observations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base


class BrokerSessionRow(Base):
    """A 24 h Sharekhan access token, sealed with a key derived from RHYTHM_AUTH__SECRET_KEY."""

    __tablename__ = "broker_sessions"
    __table_args__ = (Index("ix_broker_sessions_active", "broker", "revoked_at", "expires_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    token_sealed: Mapped[str] = mapped_column(Text, nullable=False)
    customer_sealed: Mapped[str] = mapped_column(Text, nullable=False)
    account_ref: Mapped[str] = mapped_column(String(32), nullable=False)  # masked
    exchanges: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrokerLoginStateRow(Base):
    """CSRF protection for the redirect: SHA-256 of a random state, single use, short TTL."""

    __tablename__ = "broker_login_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrokerObservationRow(Base):
    """What a read endpoint returned, as field names and types only (never values)."""

    __tablename__ = "broker_observations"
    __table_args__ = (Index("ix_broker_observations_run", "run_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    check: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(120), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # OK / EMPTY / ERROR
    detail: Mapped[str | None] = mapped_column(String(300))
    shape: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
