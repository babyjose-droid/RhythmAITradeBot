# Multi-Horizon Portfolio Orchestration

**Status:** Phase 0 design.

## 1. Model

- Three virtual ledgers per account kind (PAPER now; LIVE later): `INTRADAY`, `SWING`, `LONG_TERM`, each with allocated capital, positions, realised/unrealised P&L and theses.
- One **consolidated book** = Σ ledgers per instrument, compared to the broker (or paper engine) truth: holdings, positions, open orders, funds.

## 2. Invariants (tested)

1. Σ ledger quantities per instrument = broker quantity (per product type) — else `RECONCILIATION_BREAK`.
2. Σ ledger cash reservations + open-order reservations ≤ available funds.
3. A ledger can only sell what it owns (no borrowing from another ledger's position) unless the orchestrator records an explicit internal transfer with audit (owner-approved).
4. Paper and live books never share rows.

## 3. Orchestration cycle

1. Collect candidate decisions from each strategy run.
2. Net conflicting intents per instrument for **real exposure** (e.g. intraday BUY 100 and long-term REDUCE 100 at the same time) — each remains a separate ledger order, but the orchestrator evaluates the consolidated effect for concentration and funds, and avoids sending offsetting orders in the same cycle where configured.
3. Prioritise by horizon policy (exits/stops first, then risk-reducing, then new risk), then by score.
4. Send each resulting `OrderProposal` to the risk engine with a consolidated `RiskState`.

## 4. Evidence packet & Claude assessment (Phase 7)

Packet fields: symbol, horizon, as_of, data freshness, facts[] (value, unit, source_id, source_ts), derived_metrics[] (formula id, inputs), assumptions[], missing[], untrusted_documents[] (quoted, marked as data). Required output schema: symbol, horizon, as_of, freshness_status, observed_facts (with source refs), bull_case, bear_case, catalysts, invalidation_conditions, key_metrics_with_provenance, suggested_action ∈ decision states, rationale, confidence_explanation, key_uncertainties, change_view_conditions, evidence_sufficient (bool). Validation rejects: unknown source refs, numbers not present in packet, action outside allowed set, missing fields → fallback `NO_ACTION`/`REQUIRES_REVIEW`. Timeout/retry/token budget enforced.

## 5. Reconciliation

Scheduled (intraday: every 60 s while session open; EOD: full) and event-driven (after each ack/fill). Results in `broker_reconciliations`; breaks surface on Audit & Reconciliation screen and block via risk check R3.
