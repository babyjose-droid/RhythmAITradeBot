# Phase 1 Report — Foundation

**Date:** 2026-10-31 · **Status:** complete, awaiting owner review before Phase 2.

## Scope delivered

| Item | Where |
|---|---|
| Typed settings; `RESEARCH_ONLY` default; `LIVE` refused at start-up; immutable; feature flags (all off) | `src/rhythm/config/settings.py` |
| UTC clock + IST conversion; naive datetimes rejected | `src/rhythm/core/clock.py` |
| Structured JSON logging; redaction of tokens/keys/passwords/JWTs/URL credentials/query tokens; account IDs masked; applied to stdlib loggers too | `src/rhythm/core/logging.py`, `redaction.py` |
| PostgreSQL + SQLAlchemy 2 + Alembic; migration `0001_foundation` | `src/rhythm/db/` |
| Append-only, hash-chained `audit_events` (DB triggers block UPDATE/DELETE/TRUNCATE; advisory lock serialises the chain; verification endpoint) | `src/rhythm/audit/service.py`, migration |
| `system_health_events` (readiness transitions) | `src/rhythm/db/models/health.py` |
| FastAPI app: `/health/live`, `/health/ready` (DB, clock skew, migrations at head, mode), `/v1/system/mode`, `/v1/audit/events`, `/v1/audit/verify`; request IDs; problem+json errors; DB outage → 503 | `src/rhythm/api/` |
| Dockerfile (non-root, healthcheck), docker-compose (db → migrate → api, ports on 127.0.0.1 only) | repo root |
| CI: gitleaks, ruff, mypy --strict, import contracts, pytest with real Postgres, pip-audit, Docker build + LIVE-refusal check | `.github/workflows/ci.yml` |
| `.env.example` (placeholders only), `.gitignore`, pre-commit, Makefile | repo root |

No broker, market-data, strategy, order or LLM code exists yet. There are no order endpoints.

## Commands run and results (exact)

Environment: cloud session, Python 3.13.18 (and 3.12.3 re-run), PostgreSQL 16 local instance.

```
$ ruff check src tests            → All checks passed!
$ ruff format --check src tests   → 42 files already formatted
$ mypy  (strict, src + tests)     → Success: no issues found in 42 source files
$ lint-imports                    → Contracts: 2 kept, 0 broken.
$ pytest --cov  (REQUIRE_INTEGRATION=1, real Postgres)
                                  → 76 passed, 1 warning in 30.05s; coverage 97% (migrations & server entrypoint omitted)
$ pytest  on Python 3.12.3        → 76 passed, 1 warning in 27.12s
$ pip-audit --skip-editable       → No known vulnerabilities found
```
The 1 warning is a third-party deprecation notice (Starlette TestClient's use of httpx), not from project code.

**Smoke test** (real server process + Alembic CLI against Postgres): `alembic upgrade head` → `0001_foundation (head)`; `/health/live` 200; `/health/ready` 200 with all four checks ok; `/v1/system/mode` → `RESEARCH_ONLY`, `live_trading_available: false`; `system.started` audit event present; `/v1/audit/verify` ok. `RHYTHM_TRADING_MODE=LIVE rhythm-api` → exit code 1, "LIVE trading mode is not available". With the database stopped, `/health/live` stayed 200 and `/health/ready` returned 503 (fail closed); this also exposed DB-backed endpoints returning 500, fixed to 503 + test.

## Not verified in this environment

- **Docker image build / `docker compose up`:** the sandbox's network policy blocks Docker Hub (`registry-1.docker.io` → 403), so base images could not be pulled. The Dockerfile and compose file are untested here; the CI `docker` job and a local `make up` will be the first real runs.
- **GitHub Actions workflow:** not executed (no repository remote yet).

## Design decisions

- Sync SQLAlchemy (psycopg 3) — simpler, adequate for Phase 1 load; FastAPI runs sync routes in a threadpool.
- Migrations packaged inside `rhythm.db` so the same scripts run in tests, CLI and containers.
- The audit chain uses a fixed genesis hash; tampering by a table owner who disables triggers is still detected by `verify` (tested). Deployed environments must run the app as a non-owner role with INSERT/SELECT only on `audit_events` (added to SECURITY_MODEL).
- Redaction is deliberately over-inclusive (e.g. any key containing "token" or "session").
- No authentication yet: API binds to loopback and compose publishes on 127.0.0.1 only. Auth is Phase 8 (D-05) and **must precede any non-local hosting**.

## Next: Phase 2 (awaiting approval)

Instruments & watchlists, scrip-master snapshot model, `MarketDataProvider` protocol + mock provider, freshness monitor, data-quality events, read-only `BrokerAdapter` interface with a mock. No real Sharekhan calls without separate approval.
