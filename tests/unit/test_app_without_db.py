"""The API must stay live but report not-ready (fail closed) when the database is down."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rhythm.api.app import create_app
from tests.conftest import make_settings


def _client() -> TestClient:
    return TestClient(create_app(make_settings(log_json=True), configure_logs=False))


def test_liveness_without_database() -> None:
    with _client() as c:
        r = c.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness_fails_closed_without_database() -> None:
    with _client() as c:
        r = c.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    checks = {ch["name"]: ch["ok"] for ch in body["checks"]}
    assert checks["database"] is False
    assert checks["clock_skew"] is False
    assert checks["migrations"] is False
    assert checks["trading_mode"] is True
    # error details expose only exception class names, never connection strings
    assert "not-a-secret" not in r.text


def test_mode_endpoint_reports_research_only_and_live_unavailable() -> None:
    with _client() as c:
        r = c.get("/v1/system/mode")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_mode"] == "RESEARCH_ONLY"
    assert body["live_trading_available"] is False
    assert body["orders_permitted"] is False
    assert body["exchange_timezone"] == "Asia/Kolkata"
    assert body["server_time_exchange"].endswith("+05:30")
    assert r.headers["x-trading-mode"] == "RESEARCH_ONLY"
    assert "x-request-id" in r.headers


def test_db_backed_endpoints_return_503_not_500_when_db_down() -> None:
    with _client() as c:
        for path in ("/v1/audit/events", "/v1/audit/verify"):
            r = c.get(path)
            assert r.status_code == 503, path
            assert r.headers["content-type"] == "application/problem+json"
            assert r.headers["retry-after"] == "5"
            assert "not-a-secret" not in r.text


def test_unknown_route_404() -> None:
    with _client() as c:
        assert c.get("/v1/orders").status_code == 404  # no order endpoints exist in Phase 1
