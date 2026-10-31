"""Integration fixtures: a real PostgreSQL database given by TEST_DATABASE_URL.

Locally the tests are skipped when TEST_DATABASE_URL is unset; set REQUIRE_INTEGRATION=1
(as CI does) to turn a missing database into a failure instead of a skip.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from rhythm.db import migrate
from rhythm.db.session import create_db_engine, create_session_factory

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        if os.environ.get("REQUIRE_INTEGRATION") == "1":
            pytest.fail("TEST_DATABASE_URL is required (REQUIRE_INTEGRATION=1)")
        pytest.skip("TEST_DATABASE_URL not set")
    if "test" not in url.rsplit("/", 1)[-1]:
        pytest.fail("refusing to run destructive tests on a database whose name lacks 'test'")
    return url


@pytest.fixture(scope="session")
def engine(db_url: str) -> Iterator[Engine]:
    eng = create_db_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def migrated(db_url: str, engine: Engine) -> Iterator[Engine]:
    """Fresh schema for every test: downgrade to base, then upgrade to head."""
    engine.dispose()
    migrate.downgrade(db_url, "base")
    migrate.upgrade(db_url, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(migrated: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated)
