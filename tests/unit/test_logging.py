from __future__ import annotations

import json
import logging

import pytest

from rhythm.core.logging import configure_logging, get_logger
from tests.fakes import FAKE_JWT


def _lines(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.strip().splitlines() if line.strip()]


def test_structlog_output_is_json_and_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    get_logger("t").info(
        "ws_connect",
        url="wss://x.invalid/stream?ACCESS_TOKEN=fake-tok-1&API_KEY=fake-key-1",
        access_token=FAKE_JWT,
        customer_id="99887766",
    )
    out = capsys.readouterr().out
    assert "fake-tok-1" not in out
    assert "fake-key-1" not in out
    assert FAKE_JWT not in out
    assert "99887766" not in out
    (line,) = _lines(out)
    assert line["event"] == "ws_connect"
    assert line["level"] == "info"
    assert "timestamp" in line


def test_stdlib_loggers_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    logging.getLogger("uvicorn.error").warning("token is %s", FAKE_JWT)
    out = capsys.readouterr().out
    assert FAKE_JWT not in out
    assert _lines(out)[0]["logger"] == "uvicorn.error"


def test_exception_text_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    try:
        raise RuntimeError("failed for postgresql+psycopg://u:leaky-pw@h/db")
    except RuntimeError:
        get_logger("t").exception("boom")
    assert "leaky-pw" not in capsys.readouterr().out


def test_invalid_level_rejected() -> None:
    with pytest.raises(ValueError, match="invalid log level"):
        configure_logging("LOUD")
