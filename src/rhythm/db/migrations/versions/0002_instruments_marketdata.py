"""Instruments (point-in-time), scrip-master snapshots, watchlists, market bars, data quality.

Revision ID: 0002_instruments_marketdata
Revises: 0001_foundation
Create Date: 2026-10-31
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_instruments_marketdata"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, *, default: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "instrument_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        _ts("received_at"),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("rows", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_instrument_snapshots"),
        sa.UniqueConstraint("source", "exchange", "as_of_date", "content_hash", name="uq_snapshot"),
        sa.CheckConstraint(
            "status IN ('APPLIED', 'REJECTED', 'NO_CHANGE')", name="ck_instrument_snapshots_status"
        ),
    )

    op.create_table(
        "instruments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_code", sa.String(32), nullable=False),
        sa.Column("is_mock", sa.Boolean(), nullable=False),
        sa.Column("instrument_type", sa.String(16), nullable=False),
        sa.Column("tick_size", sa.Numeric(18, 6), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("isin", sa.String(12), nullable=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
        sa.PrimaryKeyConstraint("id", name="pk_instruments"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["instrument_snapshots.id"],
            name="fk_instruments_snapshot_id_instrument_snapshots",
        ),
        sa.CheckConstraint("tick_size > 0", name="ck_instruments_tick_positive"),
        sa.CheckConstraint("lot_size >= 1", name="ck_instruments_lot_positive"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_instruments_validity"
        ),
    )
    op.create_index(
        "uq_instruments_active",
        "instruments",
        ["exchange", "symbol"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index("ix_instruments_exchange_symbol", "instruments", ["exchange", "symbol"])

    op.create_table(
        "watchlists",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("horizon", sa.String(16), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
        sa.PrimaryKeyConstraint("id", name="pk_watchlists"),
        sa.UniqueConstraint("name", name="uq_watchlists_name"),
        sa.CheckConstraint(
            "horizon IN ('INTRADAY', 'SWING', 'LONG_TERM', 'ANY')", name="ck_watchlists_horizon"
        ),
    )

    op.create_table(
        "watchlist_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("watchlist_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        _ts("added_at", default=True),
        sa.PrimaryKeyConstraint("id", name="pk_watchlist_items"),
        sa.ForeignKeyConstraint(
            ["watchlist_id"],
            ["watchlists.id"],
            name="fk_watchlist_items_watchlist_id_watchlists",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("watchlist_id", "exchange", "symbol", name="uq_item"),
    )
    op.create_index("ix_watchlist_items_watchlist_id", "watchlist_items", ["watchlist_id"])

    op.create_table(
        "market_bars",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("interval", sa.String(16), nullable=False),
        sa.Column("bar_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(18, 4), nullable=False),
        sa.Column("high", sa.Numeric(18, 4), nullable=False),
        sa.Column("low", sa.Numeric(18, 4), nullable=False),
        sa.Column("close", sa.Numeric(18, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("is_mock", sa.Boolean(), nullable=False),
        _ts("received_at"),
        sa.PrimaryKeyConstraint("id", name="pk_market_bars"),
        sa.UniqueConstraint("exchange", "symbol", "interval", "bar_start", "source", name="uq_bar"),
        sa.CheckConstraint(
            "low <= open AND low <= close AND high >= open AND high >= close AND low > 0",
            name="ck_market_bars_ohlc",
        ),
        sa.CheckConstraint("volume >= 0", name="ck_market_bars_volume"),
    )
    op.create_index(
        "ix_market_bars_lookup", "market_bars", ["exchange", "symbol", "interval", "bar_start"]
    )

    op.create_table(
        "data_quality_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        _ts("occurred_at"),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_data_quality_events"),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="ck_data_quality_events_severity"
        ),
    )
    op.create_index("ix_data_quality_events_occurred_at", "data_quality_events", ["occurred_at"])
    op.create_index("ix_data_quality_events_kind", "data_quality_events", ["kind"])


def downgrade() -> None:
    op.drop_table("data_quality_events")
    op.drop_table("market_bars")
    op.drop_table("watchlist_items")
    op.drop_table("watchlists")
    op.drop_index("uq_instruments_active", table_name="instruments")
    op.drop_table("instruments")
    op.drop_table("instrument_snapshots")
