# Paper Trading

**Status:** Intraday PAPER trading implemented in Phase 3 (`src/rhythm/paper/`, `src/rhythm/execution/`); swing in Phase 5.

## As built (Phase 3)

- **Isolation:** `paper_accounts`, `orders`, `order_events`, `fills` and `paper_positions` are separate tables. `orders.account_kind` and `fills.account_kind` have CHECK = 'PAPER'. `order_events` and `fills` are append-only (DB triggers). API responses carry `x-account-kind: PAPER`, and the dashboard shows a PAPER banner, frames and tags.
- **Enablement:** a cycle runs only when the mode is `PAPER` **and** `features.paper_trading` is true; otherwise the request is rejected. `RESEARCH_ONLY` can run `/v1/research/cycle`, which records candidates and creates no orders.
- **Cycle:** load bars since the last cycle → for each bar, apply fills, evaluate the strategy, pass the baseline risk gate, then propose → RISK_CHECKED → SUBMITTED → simulated BROKER_ACKNOWLEDGED. The protective stop and target are linked as OCO. Square-off happens at session close. The account row is locked (`FOR UPDATE`) so two cycles cannot interleave. Each cycle writes an audit event.
- **Catch-up rule:** replayed (missed) bars can only exit. Only the newest bar, with a FRESH quote, may open a position.
- **Idempotency:** one key per signal (`strategy:version:instrument:time:type`, unique in the DB), so re-running a cycle never duplicates an order.
- **Fills:** the same fill model and versioned cost model as backtests (see BACKTESTING_METHODOLOGY.md). Long-only.
- **States shown in the UI:** Candidate (idea) → Approved, not sent → At broker, not filled → Partially filled → Filled, plus Closed without fill and Needs attention. Each state has a different label, colour and border style.

## Still to do

- Latency and rejection simulation (price band, funds) — Phase 4/9.
- Approval step — Phase 8.
- Real market data — B-06. While data is MOCK, sessions are labelled **PAPER + MOCK DATA** and excluded from live-readiness evaluation.
- Observation period (owner to define, D-04/Phase 9): e.g. ≥ 60 trading sessions, reviewing risk breaches, reconciliation breaks, alert handling, and divergence between paper and backtest.
