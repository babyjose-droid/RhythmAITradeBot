# Internal API Specification (initial)

**Status:** Phases 1–10 implemented (Phase 10 is a review; there is no live order endpoint). Since Phase 8 every route except `/health/*`, `GET /v1/auth/status` and `POST /v1/auth/login` needs a signed-in session (cookie `rhythm_session`), and every mutating request needs the `X-CSRF-Token` header. Roles: `viewer` (GET only), `approver` (+ approval decisions and kill-switch activation), `owner` (everything; policies, kill-switch release, strategy resume, ledger allocation and user admin also need a re-auth within 5 minutes → `403 reauth_required`) (health, mode, audit, instruments, watchlists, market data, data quality, strategies, backtests, PAPER accounts/orders/fills/cycles, research cycle, signals, risk controls/policies/decisions, ledgers, consolidated view, reconciliation — exact lists in the PHASE_2/3/4 reports; live OpenAPI at `/docs`); remaining rows are planned. FastAPI generates the authoritative OpenAPI document from Phase 1; this file will then summarise and link it. This is *our* API — Sharekhan's is in SHAREKHAN_INTEGRATION.md.

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
| GET | `/v1/auth/status` · POST `/v1/auth/login` · POST `/v1/auth/logout` · GET `/v1/auth/me` · POST `/v1/auth/reauth` · POST `/v1/auth/password` | 8 ✅ | password + 6-digit TOTP; login sets `rhythm_session` (HttpOnly) and `rhythm_csrf` cookies; generic 401 on any failure; 429 when rate-limited |
| GET | `/v1/users` · POST `/v1/users` · POST `/v1/users/{username}/disable` \| `/enable` \| `/reset-mfa` | 8 ✅ | owner (+ re-auth for changes); the TOTP secret is returned once on create/reset |
| GET | `/v1/approvals?status=&account_id=` · GET `/v1/approvals/{id}` · POST `/v1/approvals/{id}/decision` | 8 ✅ | queue of REQUIRES_REVIEW candidates; decision `{decision: approve\|reject, reason}` (Idempotency-Key). Approve re-runs every risk check → APPROVED + PAPER order, or FAILED with reasons |
| GET | `/v1/notifications?unread=` · GET `/v1/notifications/summary` · POST `/v1/notifications/read` | 8 ✅ | in-app inbox; webhook delivery status per notification |
| GET | `/v1/scheduler/status` · GET `/v1/scheduler/jobs` · GET `/v1/scheduler/jobs/{id}/runs` | 9 ✅ | heartbeats/leader, component health, jobs, run history |
| POST | `/v1/scheduler/jobs` · POST `/v1/scheduler/jobs/{id}/enable` | 9 ✅ | owner + re-auth; jobs are created disabled; kinds paper_cycle / events_sync / fundamentals_sync / health_check / audit_verify; schedules intraday_5m / after_close / daily_at / every_minutes |
| POST | `/v1/scheduler/jobs/{id}/disable` · POST `/v1/scheduler/jobs/{id}/run` | 9 ✅ | owner; run = manual trigger through the same path as a scheduled run (Idempotency-Key) |
| GET | `/v1/system/live-readiness` | 10 ✅ | review only: 26 gates (MET / NOT_MET / OWNER), verdict, evidence and sources; changes nothing |
| GET | `/metrics` | 9 ✅ | Prometheus text; `Authorization: Bearer $RHYTHM_METRICS__TOKEN` (local/test only without a token) |
| GET | `/v1/automation` | 8 ✅ | guards, strategies with pause state and last run, approvals policy, research gate, webhook, scheduler status |
| POST | `/v1/events/sync` · POST `/v1/events` · GET `/v1/events?watchlist_id=` | 5 ✅ | event calendar. Sync = coverage proof for R18. Manual entries need `source_ref`; `known_at` ≤ now |
| GET | `/v1/theses?account_id=&status=&horizon=` · GET `/v1/theses/{id}` | 5–6 ✅ | swing and long-term theses with append-only history (reviews and rebalances are OPEN→OPEN events with metrics = valuation history) |
| POST | `/v1/fundamentals/sync` · GET `/v1/fundamentals/{exchange}/{symbol}?quarters=` | 6 ✅ | point-in-time quarterly fundamentals (MOCK today); GET returns published quarters plus derived metrics with formulas and `is_mock`. 503 when no provider is configured |
| POST | `/v1/paper/accounts/{id}/cycle` with `strategy: swing_trend_pullback` | 5 ✅ | swing PAPER cycle (after the close). Orders and signals accept `?horizon=` |
| GET | `/v1/research/status` · GET `/v1/research/prompts` | 7 ✅ | provider, MOCK flag, gate on/off, prompt id + hash, daily token budget; prompt text and output schema |
| POST | `/v1/research/assessments` | 7 ✅ | `{signal_id, documents?}` → assess a stored candidate. Idempotency-Key required. 503 when not configured. Advisory: never creates or changes an order |
| GET | `/v1/research/assessments?symbol=&signal_id=&status=` · GET `/v1/research/assessments/{id}` | 7 ✅ | list; detail with frozen packet (sha256), validated output, validation errors, raw excerpt, usage, gate effect |
| GET | `/v1/broker/login-url` · GET `/v1/broker/callback` | 2 (read-only verification, after approval) | state validation; server-side token exchange |
| GET | `/v1/audit/events?event_type=&actor_id=&subject_type=&subject_id=&since=&until=&before_id=` | 1, 8 ✅ | filters added in Phase 8 (`event_type` exact or `prefix*`) |

Conventions: JSON; ISO-8601 UTC timestamps plus `exchange_local` where relevant; Decimals as strings.

**`Idempotency-Key` header** (8–128 chars):
- **Required** on PAPER cycle, order cancel, kill switch, pause/resume, policy and allocation changes; missing → 428.
- Optional elsewhere.
- A same-key retry replays the stored response (`idempotent-replay: true`).
- Same key with a different body → 409.

Errors use problem+json errors. No endpoint submits a live order before Phase 10.
