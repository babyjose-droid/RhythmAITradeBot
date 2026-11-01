# Paper Trading

**Status:** Intraday PAPER trading implemented in Phase 3 (`src/rhythm/paper/`, `src/rhythm/execution/`); swing in Phase 5.

## As built (Phase 3)

- **Isolation:** `paper_accounts`, `orders`, `order_events`, `fills` and `paper_positions` are separate tables. `orders.account_kind` and `fills.account_kind` have CHECK = 'PAPER'. `order_events` and `fills` are append-only (DB triggers). API responses carry `x-account-kind: PAPER`, and the dashboard shows a PAPER banner, frames and tags.
- **Enablement:** a cycle runs only when the mode is `PAPER` **and** `features.paper_trading` is true; otherwise the request is rejected. `RESEARCH_ONLY` can run `/v1/research/cycle`, which records candidates and creates no orders.
- **Phase 4 additions:**
  - each cycle reconciles before and after trading, and a break activates the kill switch;
  - UNKNOWN orders are resolved against the venue first;
  - every proposal passes the full risk engine, and the decisions are stored in `risk_checks`;
  - positions and orders belong to the INTRADAY ledger;
  - cycle and cancel calls require an `Idempotency-Key`.
- **Cycle:** load bars since the last cycle → for each bar, apply fills, evaluate the strategy, pass the baseline risk gate, then propose → RISK_CHECKED → SUBMITTED → simulated BROKER_ACKNOWLEDGED. The protective stop and target are linked as OCO. Square-off happens at session close. The account row is locked (`FOR UPDATE`) so two cycles cannot interleave. Each cycle writes an audit event.
- **Catch-up rule:** replayed (missed) bars can only exit. Only the newest bar, with a FRESH quote, may open a position.
- **Idempotency:** one key per signal (`strategy:version:instrument:time:type`, unique in the DB), so re-running a cycle never duplicates an order.
- **Fills:** the same fill model and versioned cost model as backtests (see BACKTESTING_METHODOLOGY.md). Long-only.
- **States shown in the UI:** Candidate (idea) → Approved, not sent → At broker, not filled → Partially filled → Filled, plus Closed without fill and Needs attention. Each state has a different label, colour and border style.

## Swing (Phase 5)

- **Running a cycle:** same endpoint, with `strategy: swing_trend_pullback`. Run after the close.
- **First cycle:** warms up on history and decides on the latest session. Later cycles process every unseen session; only the latest, with a FRESH daily bar, may open positions.
- **Orders:** DELIVERY product. Entries fill at the next open; unfilled entry quantity expires after one session. Stop/target are kept across sessions. Exits are re-sent until the position is flat (fills are capped at 10 % of session volume).
- **Theses:** each entry has a thesis (`PENDING_ENTRY → OPEN → INVALIDATED → CLOSED`), checked every session, with append-only history. API: `GET /v1/theses`, `GET /v1/theses/{id}`.
- **Event calendar:** sync with `POST /v1/events/sync`. R18 blocks entries without a sync in the last 7 days, or when an event is ≤ 3 days away.

## Still to do

- Latency and rejection simulation (price band, funds) — Phase 4/9.
- Approval step — Phase 8.
- Real market data — B-06. While data is MOCK, sessions are labelled **PAPER + MOCK DATA** and excluded from live-readiness evaluation.
- Observation period (owner to define, D-04/Phase 9): e.g. ≥ 60 trading sessions, reviewing risk breaches, reconciliation breaks, alert handling, and divergence between paper and backtest.
