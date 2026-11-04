# Rhythm AI Trader — Multi-Horizon Edition

AI-assisted research, portfolio decision-support and paper-trading platform for Indian cash equities (initial broker: Sharekhan). Claude is advisory only; deterministic strategy rules, portfolio accounting and an independent risk engine decide whether a candidate may become an order. **Live trading is disabled by default and is not implemented.** Nothing in this project promises returns.

**Current phase: 9 — Reliability: scheduler, monitoring & alarms, backups/restore drill, load/soak/failure tests, runbooks (complete, awaiting review before Phase 10).** Exact test results: `docs/PHASE_1_REPORT.md` … `docs/PHASE_9_REPORT.md`.

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
cd web && npm ci && npm run build && npm start      # dashboard on http://127.0.0.1:3300 (or: docker compose up web)
```

Phase 4 — risk controls (PAPER): every order passes the risk engine (R1–R20; R21 added in Phase 6); mutating calls need an `Idempotency-Key` header.

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

Phase 6 — long-term (MOCK fundamentals only until licensed data, D-03). Run after the close:

```bash
RHYTHM_FUNDAMENTALS_PROVIDER=mock ...rhythm-api              # plus the settings above
curl -X POST localhost:8000/v1/fundamentals/sync -H 'Idempotency-Key: fsync-0001' \
     -H 'content-type: application/json' -d '{"watchlist_id":3}'      # R21 needs published fundamentals
curl "localhost:8000/v1/fundamentals/NSE/MOCKE?quarters=8"            # quarters + derived metrics with formulas
curl -X POST localhost:8000/v1/paper/accounts/1/cycle -H 'Idempotency-Key: lt-0001' \
     -H 'content-type: application/json' -d '{"watchlist_id":3,"strategy":"long_term_quality_value"}'
curl "localhost:8000/v1/theses?account_id=1&horizon=LONG_TERM"
```
Dashboard: **Long-term** page.

Phase 7 — Claude research (advisory: may confirm or downgrade a rule-generated candidate; cannot create, size or send orders). MOCK provider locally; no API key needed:

```bash
RHYTHM_FEATURES__RESEARCH_SERVICE=true RHYTHM_RESEARCH_PROVIDER=mock ...rhythm-api
curl localhost:8000/v1/research/status
curl -X POST localhost:8000/v1/research/assessments -H 'Idempotency-Key: assess-0001' \
     -H 'content-type: application/json' -d '{"signal_id":1}'        # any stored candidate
curl localhost:8000/v1/research/assessments/1                         # packet, output, validation
# optional: RHYTHM_RESEARCH__GATE_PAPER_ENTRIES=true → PAPER entries need a valid CONFIRM (fail closed)
# real model: RHYTHM_RESEARCH_PROVIDER=anthropic + RHYTHM_RESEARCH__MODEL + RHYTHM_RESEARCH__API_KEY (secret store)
```
Dashboard: **AI research** page.

Phase 8 — sign-in is **on by default** (password + authenticator app). First run:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"     # put it in .env as RHYTHM_AUTH__SECRET_KEY=...
docker compose --env-file .env up -d --build
docker compose exec api rhythm-cli user-create --username owner --role owner
# → prompts for a password; prints an authenticator secret ONCE: add it to Google Authenticator / Authy / 1Password
open http://127.0.0.1:3300/login
```
Roles: `owner` (everything; risk-limit changes, kill-switch release and user admin also need a fresh password + code), `approver` (decide approvals, activate the kill switch), `viewer` (read only). REQUIRES_REVIEW candidates wait under **Approvals**; approving re-runs every risk check before any PAPER order. Notifications: in-app bell, plus an optional webhook (`RHYTHM_NOTIFICATIONS__WEBHOOK_URL`). The one-command demo below turns sign-in off (`demo.env`, local only).

Phase 9 — the stack now also runs `scheduler` (jobs you create and enable under **Automation → Scheduled jobs**; one leader, one run per slot, auto-disable after 3 failures) and `backup` (daily verified `pg_dump` into `./backups`, kept 14 days). Readiness fails closed if the scheduler heartbeat is stale. Restore drill, metrics (`/metrics` with `RHYTHM_METRICS__TOKEN`) and incident playbooks: `docs/OPERATIONS_RUNBOOK.md`. Load test: `python3 scripts/loadtest.py`; perf/soak: `pytest -m perf`, `pytest -m soak`.

**Demo in one go (local Docker, MOCK data, PAPER mode):**

```bash
docker compose --env-file .env --env-file demo.env up -d --build
python3 scripts/demo_paper.py        # instruments, watchlists, PAPER account, events, cycles, backtests
open http://127.0.0.1:3300           # dashboard
```

Safety defaults: mode `RESEARCH_ONLY`; `LIVE` makes the app refuse to start; the only orders are simulated PAPER orders (DB-enforced `account_kind='PAPER'`); no broker order method exists; API and dashboard listen on loopback only; sign-in with password + authenticator is required unless explicitly disabled for a local demo (refused in staging/production).

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
| 6 ✅ | Long-term fundamentals evidence model, thesis/valuation tracking, allocation, review schedule | Requires licensed data (D-03) or runs MOCK-labelled — built MOCK-labelled; see `docs/PHASE_6_REPORT.md` |
| 7 ✅ | Claude research service: versioned prompts, evidence packets, schema validation, timeouts/retries/budget, safe fallback | Injection & malformed-output tests; no path to orders — MOCK provider only so far; see `docs/PHASE_7_REPORT.md` |
| 8 ✅ | Full UX: 8 screens, approvals, notifications, audit views | Candidate/Approved/At-broker/Fill visually distinct — plus sign-in (D-05); see `docs/PHASE_8_REPORT.md` |
| 9 ✅ | Reliability & security: recovery, rate limits, backups, monitoring, threat review, failure injection, extended paper evaluation | Runbook rehearsed — see `docs/PHASE_9_REPORT.md` (extended PAPER evaluation on real data waits for the Sharekhan connection) |
| 10 | Live-readiness **review only** | Checklist complete; live stays off until separate explicit approval |

## Documentation

`docs/`: PHASE_1_REPORT · PHASE_2_REPORT · PHASE_3_REPORT · PHASE_4_REPORT · PHASE_5_REPORT · PHASE_6_REPORT · PHASE_7_REPORT · PHASE_8_REPORT · PHASE_9_REPORT · PRODUCT_REQUIREMENTS · SYSTEM_ARCHITECTURE · DATABASE_DESIGN · API_SPECIFICATION · SHAREKHAN_INTEGRATION · STRATEGY_SPECIFICATION · MULTI_HORIZON_PORTFOLIO · RISK_MANAGEMENT · BACKTESTING_METHODOLOGY · PAPER_TRADING · SECURITY_MODEL · COMPLIANCE_CHECKLIST · TEST_PLAN · DEPLOYMENT_GUIDE · OPERATIONS_RUNBOOK · KNOWN_LIMITATIONS
