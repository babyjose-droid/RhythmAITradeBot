from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine

from rhythm import cli

pytestmark = pytest.mark.integration


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    code = cli.main(list(argv))
    out = "\n" + capsys.readouterr().out
    # The result is pretty-printed JSON; structured log lines (single-line JSON) may precede it.
    return code, json.loads(out[out.rindex("\n{\n") + 1 :])


@pytest.fixture
def cli_env(db_url: str, migrated: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHYTHM_DATABASE_URL", db_url)
    monkeypatch.setenv("RHYTHM_ENVIRONMENT", "test")
    monkeypatch.setenv("RHYTHM_LOG_LEVEL", "ERROR")


@pytest.mark.usefixtures("cli_env")
def test_cli_mock_workflow(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(capsys, "instruments-sync", "--source", "mock")
    assert code == 0 and out["source"] == "MOCK" and out["added"] == 20
    code, out = _run(capsys, "watchlist-seed-mock", "--count", "3")
    assert code == 0 and out["items"] == ["MOCKA", "MOCKB", "MOCKC"]
    code, out = _run(capsys, "bars-backfill", "--source", "mock", "--instrument", "NSE:MOCKA")
    assert code == 0 and out["source"] == "MOCK" and out["written"] > 0


def test_cli_refuses_mock_in_production(
    capsys: pytest.CaptureFixture[str],
    db_url: str,
    migrated: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RHYTHM_DATABASE_URL", db_url)
    monkeypatch.setenv("RHYTHM_ENVIRONMENT", "production")
    monkeypatch.setenv("RHYTHM_LOG_LEVEL", "ERROR")
    code, out = _run(capsys, "instruments-sync", "--source", "mock")
    assert code == 3 and out["status"] == "REFUSED"
