# Paper Trading

**Status:** Phase 0 outline; implemented in Phase 3 (intraday) and Phase 5 (swing).

- Separate `paper_accounts`, ledgers, orders, fills; `account_kind = PAPER` enforced by DB constraints. Every screen, report and notification carries **PAPER**.
- Order flow identical to the designed live flow up to the adapter: PROPOSED → RISK_CHECKED → (APPROVAL) → SUBMITTED → simulated BROKER_ACKNOWLEDGED → PARTIALLY_FILLED/FILLED/CANCELLED/BROKER_REJECTED.
- Fill simulator uses live (or replayed) quotes: limit orders fill only when marketable; market orders fill at ask/bid ± slippage; partial fills by volume participation; simulated rejections (price band, insufficient funds) and latency; deterministic with seed in tests.
- Charges from the same versioned cost model as backtests.
- Market data used for paper must be real and authorised; if only mock data is available, the session is labelled **PAPER + MOCK DATA** and excluded from live-readiness evaluation.
- Observation period (owner to define, D-04/Phase 9): e.g. ≥ 60 trading sessions with review of risk breaches, reconciliation breaks, alert handling, and paper-vs-backtest divergence.
