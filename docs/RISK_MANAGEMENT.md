# Risk Management

**Status:** Phase 0 specification. Limits below are **illustrative paper-test placeholders, not recommendations.** Real limits must be set by the account owner with qualified oversight.

## 1. Principles

1. The risk engine is deterministic, independently testable, and has no dependency on the research/LLM module.
2. Every order proposal passes the risk engine; there is no bypass path (enforced by module contracts and tests).
3. **Fail closed.** Any missing input, exception or timeout yields `BLOCK` (or `REVIEW` where configured), never `ACCEPT`.
4. No martingale or loss-chasing: sizing inputs never include prior-trade P&L except to *reduce* risk (e.g. daily-loss stop).
5. Limits are versioned config (`risk_policies` with `version`, `effective_from`, `approved_by`). Changes require an authorised user action and create an audit event. Claude cannot change limits.

## 2. Inputs (RiskState snapshot, all timestamped)

Equity & funds (reconciled), consolidated positions, per-ledger positions, open orders (incl. UNKNOWN), today's realised/unrealised P&L per strategy, instrument metadata (tick size, lot size, circuit limits, liquidity stats), latest quote and its age, kill-switch/pause state, broker session state, reconciliation status.

## 3. Checks (each returns PASS / FAIL(reason) / REDUCE(max_qty))

| # | Check | Illustrative paper default |
|---|---|---|
| R1 | Trading mode allows orders (RESEARCH_ONLY ⇒ block; LIVE locked) | — |
| R2 | Kill switch off; strategy not paused | — |
| R3 | Broker/paper state reconciled within freshness window | ≤ 60 s intraday |
| R4 | Quote freshness for instrument | intraday ≤ 10 s; swing/long-term EOD = last session |
| R5 | Instrument eligible (in universe, active in today's scrip master, not banned/suspended, equity cash only) | — |
| R6 | Order validation: qty > 0, integer, lot multiple; price is tick multiple; within circuit band (`lowerCkt`/`upperCkt` from feed) | — |
| R7 | Planned loss per trade = qty × \|entry − stop\| + est. costs ≤ limit × equity | intraday 0.5%; swing 0.75%; long-term n/a (uses allocation) |
| R8 | Daily loss stop per strategy (realised + unrealised) | intraday 1.5% of equity |
| R9 | Strategy exposure ≤ allocated capital of its ledger | per config |
| R10 | Consolidated single-name exposure (all horizons) | ≤ 10% equity |
| R11 | Sector concentration (where data supports) | ≤ 25% equity |
| R12 | Liquidity: order qty ≤ x% of recent average volume / turnover | ≤ 1% ADV |
| R13 | Available funds (computed, incl. open orders and estimated charges) ≥ order value | — |
| R14 | Max open orders (global and per strategy) | 10 / 5 |
| R15 | Rate: orders per minute | ≤ 10/min paper |
| R16 | Idempotency: no live order with same idempotency key; no UNKNOWN order for same instrument/side | — |
| R17 | Session window: intraday entries only within configured window; no entries after cutoff | entries 09:20–14:45 IST |
| R18 | Overnight/event gate (swing): blocks or reduces when results/event within N days | configurable |
| R19 | Correlation cluster exposure (when data available) | later phase |

Outcome: all PASS ⇒ ACCEPT; any REDUCE ⇒ ACCEPT at min allowed qty (if ≥ minimum lot) else BLOCK; any FAIL ⇒ `REJECTED_BY_RISK`; policy-flagged ⇒ `REQUIRES_REVIEW`. Each check result is persisted in `risk_checks`.

## 4. Position sizing

`qty = floor( min(risk_budget / per_share_risk, exposure_cap / price, liquidity_cap, funds_cap) / lot ) × lot`, with Decimal arithmetic and costs included. Long-term sizing uses target allocation bands rather than stop distance.

## 5. Intraday end-of-session policy (illustrative)

No new entries after 14:45 IST; system-initiated exit of all intraday paper positions from 15:05 IST; any remaining intraday position at 15:15 IST raises a critical alert. Times must be checked against the broker's actual auto square-off time for the chosen product (unknown — Blocker B-02).

## 6. Idempotency & duplicate prevention

- Idempotency key = hash(strategy_version, run_id, instrument, side, signal_id). Unique constraint on `orders.idempotency_key`.
- Sharekhan exposes **no client order ID** (SHAREKHAN_INTEGRATION.md §4). On submit timeout or 5xx the order moves to `UNKNOWN`; the system queries the day order book and matches by instrument/side/qty/price/time window before any retry. Unresolved UNKNOWN blocks new orders on that instrument and alerts.

## 7. Kill switch & pauses

- **Global kill switch:** immediately blocks all new proposals; cancels all open (unfilled) paper orders; positions are **not** auto-flattened by default (configurable: `flatten_intraday = true` recommended). Activation/deactivation audited; deactivation requires an authorised user and a reason.
- **Strategy pause:** blocks new entries for that strategy; exits/stop orders still allowed.
- Automatic triggers: daily-loss stop, reconciliation break, repeated broker errors, data feed stale beyond threshold, clock skew.

## 8. Tests required (TEST_PLAN.md)

Unit tests for every check incl. boundary values; property tests (sizing never exceeds any cap; Decimal only); fail-closed tests for each missing input; kill-switch behaviour; duplicate request; UNKNOWN resolution; no-martingale property (sizing independent of previous loss except reductions).
