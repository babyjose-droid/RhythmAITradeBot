"""Foundation: append-only audit log and system health events.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=True),
        sa.Column("subject_id", sa.String(100), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.UniqueConstraint("row_hash", name="uq_audit_events_row_hash"),
        sa.CheckConstraint("actor_type IN ('system', 'user')", name="ck_audit_events_actor_type"),
        sa.CheckConstraint(
            "char_length(row_hash) = 64 AND char_length(prev_hash) = 64",
            name="ck_audit_events_hash_len",
        ),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])

    # Append-only enforcement at the database level (defence in depth; the app role
    # should additionally be granted INSERT/SELECT only in deployed environments).
    op.execute(
        """
        CREATE FUNCTION audit_events_block_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only (% blocked)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_update_delete BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_block_mutation();"
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_events_block_mutation();"
    )

    op.create_table(
        "system_health_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_system_health_events"),
        sa.CheckConstraint(
            "status IN ('ok', 'degraded', 'down')", name="ck_system_health_events_status"
        ),
    )
    op.create_index("ix_system_health_events_occurred_at", "system_health_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_system_health_events_occurred_at", table_name="system_health_events")
    op.drop_table("system_health_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events;")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_update_delete ON audit_events;")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_block_mutation();")
