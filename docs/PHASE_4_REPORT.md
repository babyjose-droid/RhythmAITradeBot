# Phase 4 Report — Risk Engine, Ledgers, Controls, Idempotency, Reconciliation

**Date:** 2026-10-31 · **Status:** complete, awaiting owner review before Phase 5.
**Data used:** MOCK only. No Sharekhan endpoint was called, no credentials were used, and no real order exists anywhere.

## Scope and acceptance criteria

| Criterion (blueprint Phase 4) | Delivered |
|---|---|
| Risk engine, all checks | `rhythm.risk.engine` (version `risk-engine-1.0`) implements R1–R17 from RISK_MANAGEMENT.md, plus R20 ("a ledger can only sell what it owns"). R18 (overnight/event gate) blocks non-intraday entries until Phase 5 supplies an event calendar. R11 (sector) and R19 (correlation) are SKIP with a stated reason, because no sector or correlation source exists yet. The engine is pure and deterministic, and **every** order passes through it: entries, strategy exits and protective stop/target orders. Sizing takes the minimum of the planned-loss budget (estimated costs included), ledger room, single-name room, ADV participation and available funds, then rounds down to whole lots |
| Every risk rule unit-tested | 74 engine tests:<br>• each rule's FAIL path (35 cases)<br>• boundaries on both sides (entry window inclusive; daily loss exactly at the limit blocks)<br>• each cap's REDUCE path<br>• lot rounding<br>• exits allowed under the kill switch, pauses, stale data and the daily loss stop<br>• exits capped at the ledger's holding<br>• policy bounds<br>• two hypothesis properties: size never exceeds any cap, and earlier losses never increase size (no martingale) |
| Fail closed | A missing input (`None`) is a FAIL, never a PASS: instrument facts, quote status, reconciliation, ADV, ledger allocation and day P&L are all covered. An exception inside any check becomes FAIL (tested by injecting errors into a gating check and a sizing check). Catch-up bars carry quote status `CATCH_UP`, which R4 rejects |
| Versioned policies | `risk_policies` table. A new version is created via `POST /v1/risk/policies` with a mandatory reason, validated against hard bounds (e.g. per-trade risk ≤ 2 %) and audited. Old versions are never edited. Every decision stores its policy and engine versions |
| Persisted decisions | `risk_checks` (append-only) stores one row per decision with all 19 check results and the exact inputs, linked to the order when one was created. Research cycles store their decisions as `SIMULATION` |
| Ledgers + consolidated view | Every PAPER account gets INTRADAY / SWING / LONG_TERM ledgers (illustrative 30/30/40 % split, D-04); existing accounts were backfilled by the migration. Orders, fills and positions carry `horizon`, and positions are keyed per ledger. Allocation changes need a reason, cannot exceed starting capital, and are audited. `GET /v1/portfolio/consolidated` sums all ledgers per instrument with the per-ledger breakdown. R10 uses the consolidated exposure |
| Kill switch / pauses | Append-only `control_events` table; current state is the latest event per target.<br>**Kill switch:**<br>• blocks every risk-increasing order (R2)<br>• cancels open entry orders in all PAPER accounts<br>• leaves protective stops/targets in place<br>• can optionally flatten intraday positions via risk-checked square-off orders<br>**Strategy pause:** blocks that strategy's entries only.<br>**Automatic triggers** (actor `system`): the daily loss stop (R8) pauses the strategy; a reconciliation break activates the kill switch. Release/resume requires a user and a reason — the system cannot release (enforced in code, tested) |
| Idempotency | Signal-level: unique order idempotency keys plus R16. API-level: an `Idempotency-Key` header is **required** on PAPER cycle, cancel, kill switch, pause/resume, policy and allocation changes. A same-key retry replays the stored response without re-running (tested: still one order, one audited cycle). Reusing a key with a different body → 409. A concurrent first use loses on the primary key and is rolled back |
| UNKNOWN scenarios | Orders now go through an `OrderVenue`. A submit timeout → `UNKNOWN`; the order is **never re-sent**. The next cycle matches it against the venue's day order book:<br>• exactly one unclaimed match → acknowledged<br>• several matches → stays UNKNOWN (ambiguous, shown as "UNKNOWN — check")<br>• no match after 5 min → cancelled as never placed<br>While UNKNOWN, new orders on that instrument are blocked (R16). Covered by unit tests (timeout after/before accept, ambiguity, rejection) and an end-to-end test on PostgreSQL |
| Reconciliation | Each PAPER cycle reconciles before and after trading (cash, per-ledger positions, order fill quantities and orphan fills). The reference is the append-only fill journal. Manual runs: `POST /v1/paper/accounts/{id}/reconcile`. Results go to the append-only `reconciliations` table. A break activates the kill switch, and R3 then blocks entries. `reconcile_with_venue` (sum of ledgers = venue position) is ready for a broker adapter and is tested with mock data only |
| Dashboard | Two new screens: **Risk & controls** and **Portfolio & reconciliation**.<br>• Risk & controls: kill switch and pause buttons (both disabled until a reason is typed), active policy, recent decisions with expandable per-check results.<br>• Portfolio & reconciliation: ledgers, consolidated positions, reconciliation history, and a "Reconcile now" button.<br>• A red **KILL SWITCH ACTIVE** banner appears on every page; it polls and refreshes immediately after a control change.<br>• Every UI mutation sends a fresh Idempotency-Key |

**New endpoints:**
- `GET /v1/risk/controls`, `POST /v1/risk/kill-switch`, `POST /v1/strategies/{name}/pause|resume`
- `GET|POST /v1/risk/policies`, `GET /v1/risk/checks`
- `GET /v1/portfolio/ledgers`, `GET /v1/portfolio/consolidated`, `PUT /v1/paper/accounts/{id}/ledgers/{horizon}`
- `POST /v1/paper/accounts/{id}/reconcile`, `GET /v1/reconciliations`

**Changed:** PAPER cycle and cancel now require `Idempotency-Key` (otherwise 428). Order output adds `horizon` and `venue_order_id`.

**Migration `0004_risk_ledgers_controls`:**
- new tables: `ledgers`, `risk_policies`, `risk_checks`, `control_events`, `reconciliations`, `idempotency_records`;
- `horizon` added to `orders`, `fills` and `paper_positions` (the positions primary key now includes `horizon`), with a long-only check;
- append-only triggers on the three new audit-type tables;
- backfills ledgers for existing accounts.

Upgrade, downgrade, re-upgrade and model drift check all ran clean.

**Removed:** the interim Phase 3 gate `rhythm/risk/baseline.py`, replaced by the engine.

**Import contracts:** 9 kept. Two are new:
- reconciliation logic is pure;
- the controls, portfolio and paper services cannot reach a broker or the API.

## Commands run and results (exact)

Cloud session, Python 3.13.18 and 3.12.3, PostgreSQL 16, Node 22:

```
$ ruff check src tests            → All checks passed!
$ ruff format --check src tests   → 119 files already formatted
$ mypy (strict)                   → Success: no issues found in 119 source files
$ lint-imports                    → Contracts: 9 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1) → 286 passed, 1 warning; coverage 94 %
    risk/ + orders/ + portfolio/ coverage → 97 % (target ≥ 90 %)
$ pytest on Python 3.12.3              → 286 passed, 1 warning
$ pip-audit --skip-editable            → No known vulnerabilities found
$ web: tsc --noEmit                    → ok
$ web: vitest run                      → Tests 5 passed (5)
$ web: next build                      → Compiled successfully (6 routes)
$ web: npm audit --omit=dev            → found 0 vulnerabilities
```
The warning is Starlette's third-party TestClient deprecation notice.

**UI smoke test.** Playwright with Chromium ran against the real API and PostgreSQL with a fixed in-session clock:
- the Phase 3 flow still passes: an order shows as at-broker, then filled;
- on Risk & controls, the activate button was disabled until a reason was typed;
- activating showed the red banner "KILL SWITCH ACTIVE — … Reason: UI drill: verify kill switch" across the app;
- releasing with a reason removed the banner;
- the decisions table showed the entry as **REDUCE** (R9 ledger room and R10 single-name cap, 80 shares ≈ ₹98k against a ₹1 L cap), and the stop, target and exit as **ACCEPT**;
- on Portfolio & reconciliation, "Reconcile now" recorded an OK manual run; ledgers were listed INTRADAY / SWING / LONG_TERM.

**Defects found during the build and fixed:**
- R3 rejected every decision because the cycle reconciles at wall-clock time, a couple of seconds *after* the bar time the decision uses. It now compares the absolute age.
- The kill-switch banner did not update after activation because the layout fetched once; it now polls and listens for control changes.
- Ledger rows sorted alphabetically; they now follow horizon order.

## Design decisions

- **Protective orders survive the kill switch.** The kill switch cancels entries but keeps stops/targets. Cancelling protection would *add* risk. Flattening is optional and goes through the engine as risk-reducing orders.
- **The system can only fail safe.** Automatic triggers can activate or pause, never release or resume.
- **R14 counts only risk-increasing open orders.** Each position's own stop/target pair would otherwise exhaust the open-order limit after two positions.
- **Venue auto square-off is not a system order.** The session-end square-off simulates the venue's automatic square-off, so it is recorded as such rather than routed through the engine. The strategy's own time exit at 15:05 is routed through the engine.
- **PAPER reconciliation compares against the fill journal.** For PAPER the fill journal is the venue's truth; for live trading the same invariant will compare against broker positions (`reconcile_with_venue`).

## Limitations carried forward

See KNOWN_LIMITATIONS.md:
- no sector, correlation or event data (R11/R19 skipped; R18 blocks swing until Phase 5);
- the circuit band is only checked when the data source provides it (mock quotes do not);
- PAPER venue latency and rejections are simulated only in tests;
- UNKNOWN resolution for PAPER uses an in-memory venue order book;
- no authentication yet (Phase 8) — "user" actions are attributed to `local-owner`;
- the cost model is still unverified.

## Next: Phase 5 (awaiting approval)

- Swing strategy (daily bars) with its own ledger;
- overnight/event gate (R18) with an event calendar source;
- thesis invalidation;
- PAPER workflow for multi-day positions.
