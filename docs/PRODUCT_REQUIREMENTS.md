# Product Requirements — Rhythm AI Trader, Multi-Horizon Edition

**Status:** Phase 0. Source: product blueprint (owner-supplied) + Phase 0 findings.

## 1. Purpose

AI-assisted research, portfolio decision-support and paper-trading platform for Indian cash equities with Sharekhan as the first broker. Claude explains and stress-tests theses; deterministic strategy code, portfolio accounting and an independent risk engine decide whether anything may become an order. No profit claims.

## 2. Users

MVP: a single account owner (also approver). Later: viewer and approver roles. Multi-client use is out of scope (would trigger regulatory review — COMPLIANCE_CHECKLIST.md §5).

## 3. Horizons

| Horizon | Holding | Inputs | Required controls |
|---|---|---|---|
| Intraday | minutes → session close | price action, trend, VWAP, volume, volatility, liquidity, opening range, session context | per-trade risk, daily loss stop, entry window, forced end-of-session exit |
| Swing | days → weeks | trend, momentum, breakout/pullback, relative strength, event calendar, gap risk | invalidation level, time stop, overnight/event gate |
| Long-term | months → years | fundamentals, valuation, earnings, cash flow, debt, sector, announcements | allocation bands, concentration, scheduled thesis review |

The same symbol may carry different decisions per horizon; ledgers are separate, broker exposure is consolidated.

## 4. Modes

`RESEARCH_ONLY` (default) — candidates displayed only. `PAPER` — simulated cash/positions/orders/fills in a separate ledger. `LIVE` — locked; not implemented before Phase 10.

## 5. Decision states vs order states

Decisions (recommendations): `BUY_CANDIDATE, ADD_CANDIDATE, HOLD, REDUCE_CANDIDATE, SELL_CANDIDATE, EXIT_CANDIDATE, NO_ACTION, REJECTED_BY_RISK, REQUIRES_REVIEW`.
Orders: `PROPOSED, RISK_CHECKED, RISK_REJECTED, APPROVAL_PENDING, APPROVED, APPROVAL_REJECTED, SUBMITTED, BROKER_ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, CANCELLED, BROKER_REJECTED, EXPIRED, UNKNOWN, RECONCILED`.
A decision is never shown as an order; an order is never shown as a fill.

## 6. UI requirements (8 screens)

Portfolio Dashboard, Intraday Workspace, Swing Trading, Long-Term Portfolio, AI Research Centre, Risk Centre, Automation Centre, Audit & Reconciliation.
Visual grammar (mandatory): **Candidate** = outlined card, grey, "IDEA"; **Approved order** = blue, "APPROVED — not sent"; **Broker-accepted** = amber, shows broker order ID, "AT BROKER — not filled"; **Fill** = solid green/red with fill qty, avg price, trade ID, time. Every value shows source timestamp; stale data shows a red STALE badge; PAPER views carry a persistent banner; MOCK data carries a MOCK badge.

## 7. Functional requirements (MVP = Phases 1–4)

- FR-1 Config & feature flags; mode default RESEARCH_ONLY.
- FR-2 Instrument registry from scrip master (daily snapshots), watchlists, point-in-time universes.
- FR-3 Market-data abstraction with provenance and freshness monitor; mock provider.
- FR-4 Intraday baseline strategy (deterministic, versioned).
- FR-5 Event-driven backtester with costs/slippage; reproducible run IDs.
- FR-6 Paper order lifecycle with partial fills/slippage.
- FR-7 Risk engine with all checks in RISK_MANAGEMENT.md; kill switch; pauses.
- FR-8 Per-horizon virtual ledgers + consolidated view + reconciliation model.
- FR-9 Audit log for every decision, approval, transition and config change.
Later phases: swing (P5), long-term (P6), Claude research (P7), full UX/notifications (P8), reliability (P9), live-readiness review (P10).

## 8. Non-functional

Correctness over speed; Decimal money; UTC storage + IST context; p95 dashboard API < 300 ms on local data; risk evaluation < 50 ms per proposal; daily backups; zero secrets in repo; tests for every risk rule and state transition.

## 9. Non-goals (MVP)

Guaranteed returns; unattended live trading; derivatives/leverage/international/other brokers; scraping prohibited sources; LLM-driven changes to limits/permissions/mode.

## 10. Product-owner decisions needed

| ID | Decision | Default proposed if no answer |
|---|---|---|
| D-01 | Where the code lives (connect a local folder / git remote) | Keep in cloud session, deliver as archive |
| D-02 | Initial universe (e.g. NIFTY 50 cash) | NIFTY 50 constituents, NSE (`NC`) |
| D-03 | Licensed data vendor(s) for intraday history & fundamentals | Mock + Sharekhan EOD only until chosen |
| D-04 | Paper capital and allocation split by horizon | ₹10,00,000 paper; 30% intraday / 30% swing / 40% long-term |
| D-05 | Dashboard auth (single owner + MFA?) and hosting (local only first?) | Local only, single owner |
| D-06 | Approval policy in PAPER (auto vs manual per order) | Auto in PAPER, manual required for anything LIVE |
| D-07 | May Claude originate long-term ideas (for human review only)? | No — rules originate, Claude assesses |
| D-08 | Claude model/cost budget per day | Configurable cap; Phase 7 |
