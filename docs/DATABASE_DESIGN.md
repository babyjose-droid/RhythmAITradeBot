# Database Design (initial)

**Status:** Implemented so far — 0001: `audit_events`, `system_health_events`; 0002: `instrument_snapshots`, `instruments` (point-in-time; partial unique index on active rows), `watchlists`, `watchlist_items` (keyed by exchange+symbol), `market_bars` (OHLC/volume check constraints, unique per source), `data_quality_events`; 0003: `paper_accounts`, `orders` (CHECK account_kind='PAPER', unique idempotency_key), `order_events` and `fills` (append-only triggers; fills CHECK PAPER), `paper_positions`, `strategy_signals` (context PAPER|RESEARCH, optional order link), `backtest_runs`, `backtest_trades`.

0004 adds:
- `ledgers` (PAPER, one per account × horizon);
- `horizon` on `orders`, `fills` and `paper_positions` (the positions primary key now includes `horizon`; CHECK quantity ≥ 0);
- `venue_order_id` on `orders`;
- `risk_policies` (immutable versions);
- `risk_checks`, `control_events` and `reconciliations` (all append-only triggers);
- `idempotency_records` (primary key: key + endpoint).

0005 adds:
- `corporate_events` (with `known_at` for point-in-time use; unique per instrument/kind/date/source);
- `event_syncs` (coverage proof);
- `theses` (PAPER only);
- `thesis_events` (append-only).

The rest is the Phase 0 proposal. PostgreSQL 16, Alembic migrations from Phase 1. All tables: `id` (UUIDv7 or bigint identity), `created_at`, `updated_at` (`timestamptz`, UTC). Money/price columns `numeric(18,4)`; quantities `bigint`. Provenance columns where data originates externally: `source`, `source_ts`, `received_ts`, `source_ref`.

## 1. Entity groups

**Identity & settings** — `users`, `roles`, `permissions`, `user_settings`.

**Broker** — `broker_connections` (broker, status, last_login_at, token_expires_at), `credential_references` (secret-store ARN/name only — never values), `broker_accounts` (masked customer id, enabled exchanges).

**Reference & market data** — `instruments` (exchange, scrip_code, trading_symbol, isin, tick_size, lot_size, inst_type; validity range `valid_from/valid_to` for point-in-time), `instrument_snapshots` (daily scrip-master archive), `watchlists`, `watchlist_items`, `market_data_points` (bars/quotes; partitioned by month; unique(instrument, interval, bar_ts, source)), `data_quality_events`.

**Strategies** — `strategy_definitions` (horizon), `strategy_versions` (semver, code hash, params JSON), `strategy_runs` (scheduled/backtest/paper, data snapshot id), `strategy_signals`.

**Research** — `research_packets` (frozen evidence JSON + hash), `research_sources` (url/ref, retrieved_at, trust=untrusted), `ai_assessments` (model, prompt_version_id, raw response ref, validated JSON, validation status, tokens, cost, latency), `prompt_versions`.

**Portfolio** — `virtual_ledgers` (horizon, account_kind PAPER|LIVE, allocated_capital), `positions` (ledger_id, instrument, qty, avg_cost, thesis_id), `cash_movements`, `portfolio_snapshots` (consolidated + per ledger), `theses` (long-term/swing thesis, invalidation, next_review_at).

**Decisions & risk** — `portfolio_decisions` (decision state, horizon, signal_id, assessment_id), `risk_policies` (versioned, approved_by), `risk_checks` (decision/order id, check code, result, inputs JSON), `risk_events`, `kill_switch_events`.

**Orders** — `orders` (ledger_id, account_kind, idempotency_key UNIQUE, state, side, qty, limit/trigger price, product, broker_order_id, rms_code), `order_events` (from_state, to_state, actor, raw_payload_ref), `fills` (order_id, trade_id UNIQUE per account_kind, qty, price, ts, charges), `broker_reconciliations` (run, scope, diffs JSON, status).

**Backtest & paper** — `backtest_runs` (strategy_version, data range, splits, seed, cost model version), `backtest_metrics`, `paper_accounts`, `paper_fills`.

**Ops** — `approvals`, `audit_events` (append-only, `prev_hash`, `row_hash`), `notifications`, `scheduled_jobs`, `system_health_events`.

## 2. Integrity rules

- `orders.account_kind` must equal its ledger's `account_kind` (trigger/FK on composite key) → paper and live can never mix.
- Order state changes only via `order_events` insert + state machine function; direct UPDATE of `orders.state` revoked from app role except through that function.
- `audit_events`: app role has INSERT/SELECT only.
- Rows with `source='MOCK'` rejected by a check constraint when `app.environment = 'production'`.
- Timestamps: UTC `timestamptz`; original exchange-local strings stored in `source_ts_raw` where relevant.

## 3. Retention

Market data: indefinite (partitioned). Raw broker payloads: 7 years (to confirm against record-keeping requirements). Tokens: never persisted unencrypted; purged at expiry.
