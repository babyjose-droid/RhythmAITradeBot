"""Phase 9 performance budgets (PRODUCT_REQUIREMENTS §8). Run with ``pytest -m perf``.

* risk evaluation < 50 ms per proposal (p95);
* dashboard API p95 < 300 ms on local data (in-process client, seeded database).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable

import pytest
from sqlalchemy import Engine

from rhythm.risk.engine import evaluate
from rhythm.risk.policy import DEFAULT_POLICY
from tests.integration.test_phase9_api import Env9
from tests.unit.test_risk_engine import prop, state

pytestmark = [pytest.mark.integration, pytest.mark.perf]


def p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[18]


def timed(fn: Callable[[], object], n: int) -> list[float]:
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return out


def test_risk_evaluation_budget() -> None:
    p, s = prop(), state()
    samples = timed(lambda: evaluate(p, s, DEFAULT_POLICY), 2000)
    med, top = statistics.median(samples) * 1e3, p95(samples) * 1e3
    print(f"\nrisk evaluate: p50 {med:.3f} ms, p95 {top:.3f} ms")
    assert p95(samples) < 0.050


ENDPOINTS = [
    "/v1/watchlists",
    "/v1/paper/accounts",
    "/v1/paper/accounts/{aid}",
    "/v1/paper/accounts/{aid}/orders",
    "/v1/signals?limit=100",
    "/v1/risk/controls",
    "/v1/portfolio/ledgers?account_id={aid}",
    "/v1/portfolio/consolidated?account_id={aid}",
    "/v1/theses?account_id={aid}",
    "/v1/approvals",
    "/v1/notifications/summary",
    "/v1/automation",
    "/v1/audit/events?limit=100",
    "/v1/scheduler/status",
    "/v1/reconciliations?account_id={aid}",
]


def test_dashboard_api_latency_budget(db_url: str, migrated: Engine) -> None:
    e = Env9(db_url, migrated)
    with e.c as c:
        wid, aid = e.seed()
        for _ in range(3):  # some history: orders, signals, theses, reconciliations
            c.post(
                f"/v1/paper/accounts/{aid}/cycle",
                json={"watchlist_id": wid, "strategy": "long_term_quality_value"},
            )
        report = []
        for path in ENDPOINTS:
            url = path.format(aid=aid)
            assert c.get(url).status_code == 200, url
            samples = timed(lambda: c.get(url), 40)  # noqa: B023 — called immediately
            report.append((p95(samples), url))
        print("\nAPI p95 (ms):")
        for v, url in sorted(report, reverse=True):
            print(f"  {v * 1e3:7.1f}  {url}")
        assert max(v for v, _ in report) < 0.300
