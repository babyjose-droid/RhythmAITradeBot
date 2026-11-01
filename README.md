# Rhythm AI Trader — Multi-Horizon Edition

AI-assisted research, portfolio decision-support and paper-trading platform for Indian cash equities (initial broker: Sharekhan). Claude is advisory only; deterministic strategy rules, portfolio accounting and an independent risk engine decide whether a candidate may become an order. **Live trading is disabled by default and is not implemented.** Nothing in this project promises returns.

**Current phase: 5 — Swing strategy, event gate, thesis tracking, multi-day PAPER (complete, awaiting review before Phase 6).** Exact test results: `docs/PHASE_1_REPORT.md` … `docs/PHASE_5_REPORT.md`.

## Quick start (local)

```bash
cp .env.example .env            # placeholders only; set a local POSTGRES_PASSWORD
make install                    # venv + dev dependencies (Python 3.12+)
docker compose up --build -d    # Postgres → migrations → API on http://127.0.0.1:8000
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/v1/system/mode
```

Without Docker: run PostgreSQL 16 yourself, set `RHYTHM_DATABASE_URL`, then `make migrate` and `.venv/bin/rhythm-api`.

Tests: `make lint typecheck contracts` and `TEST_DATABASE_URL=postgresql+psycopg://user:pw@localhost:5432/rhythm_test make test` (the database name must contain `test`; it is wiped per test).

Try the Phase 2 features with synthetic MOCK data (local only):

```bash
export RHYTHM_DATABASE_URL=postgresql+psycopg://rhythm:<pw>@127.0.0.1:55432/rhythm
.venv/bin/rhythm-cli instruments-sync --source mock
.venv/bin/rhythm-cli watchlist-seed-mock --count 3
.venv/bin/rhythm-cli bars-backfill --source mock --instrument NSE:MOCKA
RHYTHM_MARKET_DATA_PROVIDER=mock RHYTHM_BROKER_ADAPTER=mock .venv/bin/rhythm-api
curl "http://127.0.0.1:8000/v1/marketdata/freshness?watchlist_id=1"
```

Phase 3 — backtest and PAPER trading on MOCK data (local only):

```bash
.venv/bin/rhythm-cli backtest-run --symbols NSE:MOCKA,NSE:MOCKB --days 30   # stores a run; results are pipeline checks only
.venv/bin/rhythm-cli paper-account-create --name demo --cash 1000000
RHYTHM_TRADING_MODE=PAPER RHYTHM_FEATURES__PAPER_TRADING=true RHYTHM_MARKET_DATA_PROVIDER=mock RHYTHM_BROKER_ADAPTER=mock .venv/bin/rhythm-api
cd web && npm ci && npm run build && npm start      # dashboard on http://127.0.0.1:3000 (or: docker compose up web)
```

Phase 4 — risk controls (PAPER): every order passes the risk engine (R1–R20); mutating calls need an `Idempotency-Key` header.

```bash
curl -X POST http://127.0.0.1:8000/v1/risk/kill-switch -H 'Idempotency-Key: drill-0001' \
     -H 'content-type: application/json' -d '{"action":"activate","reason":"drill"}'
curl http://127.0.0.1:8000/v1/risk/controls
curl "http://127.0.0.1:8000/v1/portfolio/consolidated?account_id=1"
```
Dashboard: **Risk & controls** and **Portfolio & reconciliation** screens.

Phase 5 — swing (daily bars, multi-day PAPER positions with theses). Run after the close:

```bash
RHYTHM_EVENT_PROVIDER=mock ...rhythm-api                     # plus the PAPER settings above
curl -X POST localhost:8000/v1/events/sync -H 'Idempotency-Key: sync-0001' \
     -H 'content-type: application/json' -d '{"watchlist_id":1}'      # R18 needs a synced calendar
curl -X POST localhost:8000/v1/paper/accounts/1/cycle -H 'Idempotency-Key: swing-0001' \
     -H 'content-type: application/json' -d '{"watchlist_id":1,"strategy":"swing_trend_pullback"}'
curl "localhost:8000/v1/theses?account_id=1"
.venv/bin/rhythm-cli backtest-run --strategy swing_trend_pullback --days 400
```
Dashboard: **Swing workspace**.

Safety defaults: mode `RESEARCH_ONLY`; `LIVE` makes the app refuse to start; the only orders are simulated PAPER orders (DB-enforced `account_kind='PAPER'`); no broker order method exists; API and dashboard listen on loopback only (no auth until Phase 8).

## Phase 0 findings (2026-10-31)

- **Repository:** greenfield. No existing code, dependencies, tests or conventions were found in the session workspace; the linked Mac has no folder connected to this session, and no project docs existed in the "Trade BOT" project. → Blueprint stack adopted (SYSTEM_ARCHITECTURE.md).
- **Runtime available for development:** Python 3.13, Node 22, Docker, PostgreSQL client, Redis.
- **Sharekhan API:** official docs read in a rendered browser + official SDK source. REST base `https://api.sharekhan.com/skapi/`, browser-redirect login → request token → AES-GCM transform → access token (JWT, 24 h, no refresh documented); orders, reports, positions, holdings, funds, historical candles (intraday only last 7 days, unadjusted), scrip master (current day only), WebSocket feed + order acks. No sandbox, no client order ID, rate limits unclear, eight doc/SDK discrepancies. Official SDK disables TLS verification on WebSocket — will not be used. Full detail: `docs/SHAREKHAN_INTEGRATION.md`.
- **Regulatory:** SEBI retail-algo circular (04-Feb-2025) confirmed to exist; requirements (static IP, algo ID, tagging) **not verified** — blocks any live path. `docs/COMPLIANCE_CHECKLIST.md`.
- **Blockers:** B-01…B-11 in `docs/KNOWN_LIMITATIONS.md`. **Decisions needed:** D-01…D-08 in `docs/PRODUCT_REQUIREMENTS.md` §10.

## Phased plan

| Phase | Scope | Acceptance criteria |
|---|---|---|
| 0 ✅ | Inspect, verify broker API, plan | Docs in `docs/`; blockers & decisions listed; **owner approval** |
| 1 ✅ | Foundation: repo layout, settings & feature flags (mode default RESEARCH_ONLY, LIVE refused), Postgres + Alembic, structured redacted logging, audit log, `/health`, CI (ruff, mypy, pytest, gitleaks), Docker Compose, `.env.example` | `docker compose up` works; migrations up/down; tests run green **and results reported verbatim**; LIVE start-up refusal tested |
| 2 ✅ | Instruments/watchlists, scrip-master snapshots, MarketDataProvider + mock, freshness monitor, data-quality events, Sharekhan adapter **read-only** behind interface (mocks; supervised read-only verification only with approval) | Stale-data detection tests; provenance on every datum; MOCK labelled |
| 3 ✅ | Intraday baseline strategy, event-driven backtester with cost model, paper order lifecycle & fill simulator, first dashboard screens | Deterministic backtests; look-ahead canaries; paper state-machine tests; PAPER banners |
| 4 ✅ | Risk engine (all checks), ledgers + consolidated view, kill switch/pauses, idempotency, reconciliation | Every risk rule unit-tested; fail-closed tests; duplicate/UNKNOWN scenarios |
| 5 ✅ | Swing strategy, overnight/event gate, thesis invalidation, paper workflow | Backtests + paper tests |
| 6 | Long-term fundamentals evidence model, thesis/valuation tracking, allocation, review schedule | Requires licensed data (D-03) or runs MOCK-labelled |
| 7 | Claude research service: versioned prompts, evidence packets, schema validation, timeouts/retries/budget, safe fallback | Injection & malformed-output tests; no path to orders |
| 8 | Full UX: 8 screens, approvals, notifications, audit views | Candidate/Approved/At-broker/Fill visually distinct |
| 9 | Reliability & security: recovery, rate limits, backups, monitoring, threat review, failure injection, extended paper evaluation | Runbook rehearsed |
| 10 | Live-readiness **review only** | Checklist complete; live stays off until separate explicit approval |

## Documentation

`docs/`: PHASE_1_REPORT · PHASE_2_REPORT · PHASE_3_REPORT · PHASE_4_REPORT · PHASE_5_REPORT · PRODUCT_REQUIREMENTS · SYSTEM_ARCHITECTURE · DATABASE_DESIGN · API_SPECIFICATION · SHAREKHAN_INTEGRATION · STRATEGY_SPECIFICATION · MULTI_HORIZON_PORTFOLIO · RISK_MANAGEMENT · BACKTESTING_METHODOLOGY · PAPER_TRADING · SECURITY_MODEL · COMPLIANCE_CHECKLIST · TEST_PLAN · DEPLOYMENT_GUIDE · OPERATIONS_RUNBOOK · KNOWN_LIMITATIONS
