# Test Plan

**Status:** Phase 1 — 76 tests (unit + integration on real PostgreSQL), all passing on 2026-10-31; exact output in PHASE_1_REPORT.md. Results are recorded per phase.

## Phase 1 coverage

Settings safety (LIVE refusal, defaults, placeholder credentials, immutability), redaction (incl. property tests), logging redaction for structlog + stdlib + exceptions, clock/IST, audit hash determinism, migrations up/down + model/migration drift check, audit append-only triggers, tamper & deletion detection, concurrent appends (80 rows, 8 threads), rollback leaves no audit row, readiness fail-closed (DB down, migrations missing, clock skew), 503 on DB outage, audit pagination.

## Layers

- **Unit** (pytest, hypothesis): strategies, sizing, every risk check incl. boundaries, order state machine (all legal/illegal transitions), ledger aggregation, cost model, freshness logic, schema validation of LLM output, redaction filter.
- **Integration** (pytest + Postgres in Docker): migrations up/down, repositories, API endpoints, mock broker & mock market-data providers, scheduler jobs.
- **Scenario / failure injection**: duplicate requests; submit timeout → UNKNOWN → reconciliation; partial fills; broker rejection; 403 token expiry; 429 backoff; 502 RMS down; stale quotes; missing prices; WebSocket drop/gap; risk-engine exception (must block); kill switch mid-session; clock skew; reconciliation break.
- **Contract tests**: Sharekhan adapter against recorded, **redacted** fixtures matching documented shapes; marked `unverified` until real read-only responses are captured.
- **Safety tests**: research module cannot import broker/orders (import-linter); LIVE mode refuses to start; paper/live mixing rejected by DB; prompt-injection fixtures yield no action change beyond allowed set; no secrets in repo (gitleaks).
- **Backtest determinism**: same run inputs ⇒ identical outputs/hash; look-ahead canary tests.
- **Frontend** (Phase 3/8): component tests for state badges (Candidate/Approved/At broker/Fill), STALE/PAPER/MOCK banners; Playwright smoke.

## Gates

CI on every change: ruff, mypy, pytest (unit+integration), coverage report (target ≥ 90% on `risk/`, `orders/`, `portfolio/`), gitleaks, pip-audit. No live credentials in tests; fixtures deterministic.
