# Test Plan

**Status:** Phase 4: 286 Python tests (unit + integration on real PostgreSQL) and 5 web tests, all passing on 2026-10-31. Exact output is in the PHASE_1–4 reports.

## Phase 4 coverage

- **Risk engine:** every rule's FAIL path (35 cases), boundaries, the REDUCE path for each cap, lot rounding, and the review threshold.
- **Exits:** allowed under the kill switch, pauses, stale data and the daily loss stop; capped at the ledger's holding.
- **Fail-closed:** missing inputs, plus injected exceptions in a gating check and in a sizing check.
- **Hypothesis properties:** size never exceeds any cap; no martingale.
- **Policy:** bounds and round-trip.
- **UNKNOWN orders:** timeout after/before accept, ambiguous match, rejection, and R16 blocking.
- **Runner:** gating by kill switch, pause, missing facts and a stale quote.
- **Reconciliation:** OK, every break kind, negative journal position, consolidation and the venue invariant.
- **Integration:**
  - ledger creation and allocation audit;
  - idempotency required / replay / conflict;
  - decision persistence and append-only triggers;
  - policy versioning used by the next cycle;
  - kill switch cancel / block / release with reason;
  - pause / resume;
  - daily loss auto-pause;
  - reconciliation break tripping the kill switch;
  - consolidated view;
  - UNKNOWN end to end without a re-send.
- **Web:** risk outcome and check tones fail closed.
- **Manual:** a Playwright smoke test of the new screens.

## Phase 3 coverage

- **Strategy:** breakout, VWAP filter, volume filter, wide-stop rejection, time exit, one entry per day.
- **Look-ahead guards:** future bars, foreign bars, unordered bars. Backtester **prefix invariance** and **future-tampering** tests.
- **Order state machine:** every illegal transition raises; display categories are checked.
- **Cost model:** components per side and product.
- **Fill model:** slippage clamped to the bar, limit marketability, stop gap, participation cap and partial fills.
- **Paper book:** idempotent propose, OCO, same-bar priority, short refusal, expiry and force-close.
- **Risk gate:** accept, reduce and block paths, stale-data block, exits always allowed.
- **Backtests:** deterministic run hash, split overlap rejection, metrics.
- **API integration:**
  - full lifecycle candidate → order → fill;
  - PAPER-only DB constraint and append-only triggers;
  - cancel;
  - research mode creates no orders;
  - catch-up bars cannot open positions;
  - backtest API.
- **Migration 0003:** up and down.
- **Web (vitest):** badge mapping, decision labels, freshness fails closed. A Playwright smoke test of the dashboard was run manually against the real API (see PHASE_3_REPORT.md).

## Phase 2 coverage

Instrument keys, tick rounding, session calendar boundaries/weekends/holidays; snapshot diff (add/change/delist, empty, duplicates, bad ticks, wrong exchange, mass-removal guard, hash order-independence); point-in-time registry, idempotent re-apply, out-of-order/same-day conflict rejection with persisted records and quality events; MOCK refusal in staging/production (settings, registry, bars, CLI); bar/quote validation; freshness per horizon incl. after-close and weekend; mock providers' determinism and every failure mode; read-only broker protocol has no order methods; Sharekhan envelope status mapping and documented scrip-master shape; watchlist API with audit-chain verification; freshness transitions recorded once with recovery; provenance of stored bars; CLI workflow.

## Phase 1 coverage

Settings safety (LIVE refusal, defaults, placeholder credentials, immutability), redaction (incl. property tests), logging redaction for structlog + stdlib + exceptions, clock/IST, audit hash determinism, migrations up/down + model/migration drift check, audit append-only triggers, tamper & deletion detection, concurrent appends (80 rows, 8 threads), rollback leaves no audit row, readiness fail-closed (DB down, migrations missing, clock skew), 503 on DB outage, audit pagination.

## Layers

- **Unit** (pytest, hypothesis): strategies, sizing, every risk check incl. boundaries, order state machine (all legal/illegal transitions), ledger aggregation, cost model, freshness logic, schema validation of LLM output, redaction filter.
- **Integration** (pytest + Postgres in Docker): migrations up/down, repositories, API endpoints, mock broker & mock market-data providers, scheduler jobs.
- **Scenario / failure injection**: duplicate requests; submit timeout → UNKNOWN → reconciliation; partial fills; broker rejection; 403 token expiry; 429 backoff; 502 RMS down; stale quotes; missing prices; WebSocket drop/gap; risk-engine exception (must block); kill switch mid-session; clock skew; reconciliation break.
- **Contract tests**: Sharekhan adapter against recorded, **redacted** fixtures matching documented shapes; marked `unverified` until real read-only responses are captured.
- **Safety tests**: research module cannot import broker/orders (import-linter); LIVE mode refuses to start; paper/live mixing rejected by DB; prompt-injection fixtures yield no action change beyond allowed set; no secrets in repo (gitleaks).
- **Backtest determinism**: same run inputs ⇒ identical outputs/hash; look-ahead canary tests.
- **Frontend** (Phase 3/8): component tests for state badges (Candidate/Approved/At broker/Fill), STALE/PAPER/MOCK banners; Playwright smoke.

## Gates

CI on every change: ruff, mypy, pytest (unit+integration), coverage report (target ≥ 90% on `risk/`, `orders/`, `portfolio/`), gitleaks, pip-audit. No live credentials in tests; fixtures deterministic.
