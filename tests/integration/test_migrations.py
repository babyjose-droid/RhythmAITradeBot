from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect

from rhythm.db import migrate, models  # noqa: F401  # models register on metadata
from rhythm.db.base import Base

pytestmark = pytest.mark.integration


def test_upgrade_creates_schema_at_head(migrated: Engine) -> None:
    tables = set(inspect(migrated).get_table_names())
    assert {"audit_events", "system_health_events", "alembic_version"} <= tables
    assert migrate.current_revision(migrated) == migrate.head_revision()


def test_downgrade_then_upgrade_roundtrip(db_url: str, migrated: Engine) -> None:
    migrate.downgrade(db_url, "base")
    migrated.dispose()
    assert "audit_events" not in inspect(migrated).get_table_names()
    assert migrate.current_revision(migrated) is None
    migrate.upgrade(db_url, "head")
    migrated.dispose()
    assert "audit_events" in inspect(migrated).get_table_names()


def test_models_match_migrations(migrated: Engine) -> None:
    """Autogenerate diff must be empty: ORM models and migration scripts agree."""
    with migrated.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
