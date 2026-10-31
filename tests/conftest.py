"""Shared fixtures. No real credentials are used anywhere in the test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from rhythm.config import Settings, get_settings

UNREACHABLE_DB_URL = "postgresql+psycopg://nobody:not-a-secret@127.0.0.1:1/none"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip RHYTHM_* variables so tests never inherit a developer's local config."""
    for key in list(os.environ):
        if key.startswith("RHYTHM_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"database_url": UNREACHABLE_DB_URL, "environment": "test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)
