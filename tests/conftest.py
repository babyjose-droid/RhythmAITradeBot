"""Shared fixtures. No real credentials are used anywhere in the test suite."""

from __future__ import annotations

import os
import uuid
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


def auto_idempotency(client: Any) -> None:
    """Give every POST/PUT a fresh Idempotency-Key unless the test sets one itself."""

    def hook(request: Any) -> None:
        if request.method in ("POST", "PUT") and "idempotency-key" not in request.headers:
            request.headers["Idempotency-Key"] = f"test-{uuid.uuid4().hex}"

    client.event_hooks = {"request": [hook], "response": []}
