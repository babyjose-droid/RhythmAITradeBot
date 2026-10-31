# Internal API Specification (initial)

**Status:** Phases 1–3 implemented (health, mode, audit, instruments, watchlists, market data, data quality, strategies, backtests, PAPER accounts/orders/fills/cycles, research cycle, signals — exact lists in PHASE_2_REPORT.md and PHASE_3_REPORT.md; live OpenAPI at `/docs`); remaining rows are planned. FastAPI generates the authoritative OpenAPI document from Phase 1; this file will then summarise and link it. This is *our* API — Sharekhan's is in SHAREKHAN_INTEGRATION.md.

| Method | Path | Phase | Notes |
|---|---|---|---|
| GET | `/health/live`, `/health/ready` | 1 | ready = DB reachable, migrations current, clock skew OK |
| GET | `/v1/system/mode` | 1 | current TradingMode, live gate status (always locked pre-P10) |
| GET/POST | `/v1/instruments`, `/v1/watchlists` | 2 | |
| GET | `/v1/marketdata/{instrument}/bars?interval=&start=&end=` | 2 | each bar with provenance |
| GET | `/v1/marketdata/freshness` | 2 | per-feed staleness |
| GET | `/v1/strategies` | 3 ✅ | name, version, params, params hash |
| POST/GET | `/v1/backtests` · GET `/v1/backtests/{id}` | 3 ✅ | `data_source` db\|provider, named non-overlapping splits; result has run/data hash, cost model, metrics per split, trade list |
| POST/GET | `/v1/paper/accounts` · GET `/{id}`, `/{id}/orders`, `/{id}/fills` · POST `/{id}/cycle` | 3 ✅ | PAPER-only; responses carry `x-account-kind: PAPER`; cycle requires mode PAPER + `features.paper_trading` |
| GET | `/v1/paper/orders/{id}/events` · POST `/v1/paper/orders/{id}/cancel` | 3 ✅ | append-only event history |
| POST | `/v1/research/cycle` | 3 ✅ | evaluates strategy, records candidates, creates **no** orders |
| GET | `/v1/signals` | 3 ✅ | candidates (`kind: CANDIDATE`) — never orders; linked `order_id` if one was created |
| GET/PUT | `/v1/risk/policies` | 4 | PUT requires owner + reason; creates new version |
| POST | `/v1/risk/kill-switch` (activate/release) | 4 | audited |
| POST | `/v1/strategies/{id}/pause` / `resume` | 4 | |
| GET | `/v1/portfolio/consolidated`, `/v1/portfolio/ledgers/{horizon}` | 4 | |
| GET | `/v1/reconciliations` | 4 | |
| POST | `/v1/approvals/{order_id}` | 4/8 | approve/reject with reason |
| GET | `/v1/research/assessments/{id}` | 7 | packet, sources, output, validation result |
| GET | `/v1/broker/login-url` · GET `/v1/broker/callback` | 2 (read-only verification, after approval) | state validation; server-side token exchange |
| GET | `/v1/audit/events` | 1+ | |

Conventions: JSON, ISO-8601 UTC timestamps plus `exchange_local` where relevant, Decimal as strings, idempotency key header on all POSTs, problem+json errors. No endpoint submits a live order before Phase 10.
