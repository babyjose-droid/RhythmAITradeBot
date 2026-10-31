"""Programmatic Alembic helpers (used by tests, readiness checks and the CLI)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

SCRIPT_LOCATION = "rhythm.db:migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    # ConfigParser interpolation: escape '%' in URLs (e.g. URL-encoded passwords).
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


def head_revision() -> str | None:
    script = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://unused"))
    return script.get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
