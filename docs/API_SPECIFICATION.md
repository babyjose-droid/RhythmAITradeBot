# Internal API Specification (initial)

**Status:** Phases 1–5 implemented (health, mode, audit, instruments, watchlists, market data, data quality, strategies, backtests, PAPER accounts/orders/fills/cycles, research cycle, signals, risk controls/policies/decisions, ledgers, consolidated view, reconciliation — exact lists in the PHASE_2/3/4 reports; live OpenAPI at `/docs`); remaining rows are planned. FastAPI generates the authoritative OpenAPI document from Phase 1; this file will then summarise and link it. This is *our* API — Sharekhan's is in SHAREKHAN_INTEGRATION.md.

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
| GET/POST | `/v1/risk/policies` | 4 ✅ | POST creates a new version (reason required, hard bounds, audited); never edits old ones |
| GET | `/v1/risk/controls` | 4 ✅ | kill switch, paused strategies, history |
| POST | `/v1/risk/kill-switch` (activate/release) | 4 ✅ | reason required; activation cancels entry orders, optional `flatten_intraday`; audited |
| POST | `/v1/strategies/{name}/pause` / `resume` | 4 ✅ | reason required; audited |
| GET | `/v1/risk/checks?account_id=&outcome=&order_id=` | 4 ✅ | every engine decision with all check results and inputs |
| GET | `/v1/portfolio/ledgers?account_id=`, `/v1/portfolio/consolidated?account_id=` | 4 ✅ | per-ledger summary; consolidated per instrument with ledger breakdown |
| PUT | `/v1/paper/accounts/{id}/ledgers/{horizon}` | 4 ✅ | allocation change; reason required; total ≤ starting capital |
| POST | `/v1/paper/accounts/{id}/reconcile` · GET `/v1/reconciliations?account_id=` | 4 ✅ | a break activates the kill switch |
| POST | `/v1/approvals/{order_id}` | 4/8 | approve/reject with reason |
| POST | `/v1/events/sync` · POST `/v1/events` · GET `/v1/events?watchlist_id=` | 5 ✅ | event calendar. Sync = coverage proof for R18. Manual entries need `source_ref`; `known_at` ≤ now |
| GET | `/v1/theses?account_id=&status=` · GET `/v1/theses/{id}` | 5 ✅ | swing theses with append-only history |
| POST | `/v1/paper/accounts/{id}/cycle` with `strategy: swing_trend_pullback` | 5 ✅ | swing PAPER cycle (after the close). Orders and signals accept `?horizon=` |
| GET | `/v1/research/assessments/{id}` | 7 | packet, sources, output, validation result |
| GET | `/v1/broker/login-url` · GET `/v1/broker/callback` | 2 (read-only verification, after approval) | state validation; server-side token exchange |
| GET | `/v1/audit/events` | 1+ | |

Conventions: JSON; ISO-8601 UTC timestamps plus `exchange_local` where relevant; Decimals as strings.

**`Idempotency-Key` header** (8–128 chars):
- **Required** on PAPER cycle, order cancel, kill switch, pause/resume, policy and allocation changes; missing → 428.
- Optional elsewhere.
- A same-key retry replays the stored response (`idempotent-replay: true`).
- Same key with a different body → 409.

Errors use problem+json errors. No endpoint submits a live order before Phase 10.
