# Internal API Specification (initial)

**Status:** Phase 0 sketch. FastAPI generates the authoritative OpenAPI document from Phase 1; this file will then summarise and link it. This is *our* API — Sharekhan's is in SHAREKHAN_INTEGRATION.md.

| Method | Path | Phase | Notes |
|---|---|---|---|
| GET | `/health/live`, `/health/ready` | 1 | ready = DB reachable, migrations current, clock skew OK |
| GET | `/v1/system/mode` | 1 | current TradingMode, live gate status (always locked pre-P10) |
| GET/POST | `/v1/instruments`, `/v1/watchlists` | 2 | |
| GET | `/v1/marketdata/{instrument}/bars?interval=&start=&end=` | 2 | each bar with provenance |
| GET | `/v1/marketdata/freshness` | 2 | per-feed staleness |
| POST | `/v1/backtests` · GET `/v1/backtests/{id}` | 3 | |
| GET | `/v1/paper/orders`, `/v1/paper/fills`, `/v1/paper/positions` | 3 | PAPER-only routes |
| GET | `/v1/decisions?horizon=` | 3 | candidates — never orders |
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
