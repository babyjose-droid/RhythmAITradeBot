# Test Plan

**Status:** Phase 5: 322 Python tests (unit + integration on real PostgreSQL) and 6 web tests, all passing on 2026-11-01. Exact output is in the PHASE_1–5 reports.

## Phase 8 coverage

- Unit (`test_phase8_core.py`, 10):
  - TOTP against the RFC 6238 test vectors, drift window, replay, format and provisioning URI;
  - scrypt hashing and password policy;
  - Fernet secret box (wrong key fails);
  - login rate limiter;
  - risk engine: approval satisfies only REVIEW, and a FAIL (kill switch) still blocks;
  - approval expiry per horizon.
- Integration on PostgreSQL (`test_phase8_api.py`, 10):
  - login, session cookie and CSRF enforcement; actions attributed to the user; failed logins audited; logout revokes;
  - code replay, lockout and idle timeout;
  - roles (viewer read-only, approver kill-switch activate but not release), re-auth required and accepted, user admin (secret shown once, last owner protected, weak password refused);
  - password change revokes other sessions;
  - REVIEW candidate queued once, notification raised, approved → risk re-run → PAPER order + thesis, double approval refused, audit chain intact;
  - FAILED re-check under the kill switch, reject, expiry, mark-all-read;
  - approver decides, viewer cannot;
  - webhook outbox: delivery, severity filter, backoff to FAILED, no URL in the payload;
  - automation overview and audit filters;
  - CLI user creation.
- Settings: auth cannot be disabled and cookies cannot be insecure outside local/test; a real secret key is required; webhooks must use https.
- Web (`status.test.ts`): approval, severity and role helpers.

## Phase 7 coverage

- Unit (`test_phase7_core.py`, 34):
  - packet provenance, determinism and point-in-time correctness;
  - untrusted-document sanitising (delimiters, control characters, length);
  - prompt rules;
  - each validation rejection (malformed, non-object, oversize, extra fields such as `quantity`, unknown actions, wrong symbol/as_of, unknown sources, metric/source mismatch, numbers not in the packet, CONFIRM without evidence);
  - rounding and percent tolerance;
  - gate truth table (only confirm or downgrade; exits never blocked);
  - MOCK provider determinism and downgrades;
  - **prompt-injection fixture**;
  - Anthropic request shape, retries, timeout and auth errors (no key leakage, no retry on 401);
  - settings safety;
  - runner advisor hook (hold → no order; CONFIRM → identical quantity; exits never consulted).
- Integration on PostgreSQL (`test_phase7_api.py`, 9):
  - not configured → 503;
  - end-to-end assessment with idempotent replay (no second provider call);
  - injection with a compromised MOCK model → INVALID and held;
  - budget exhausted → no call;
  - PAPER gate: HOLD → no order or thesis; CONFIRM → the rules' order goes through; provider timeout → fail closed;
  - research tables append-only.
- Web (`status.test.ts`): a research failure or downgrade is never shown as an approval.

## Phase 6 coverage

- Unit (`test_phase6_core.py`, 29): snapshot validation, derived metrics and formulas, point-in-time selection with restatements, MOCK determinism and publication lag, look-ahead guard, entry with thesis, each blocked-entry reason, invalidation exit, hold, review rebalances (stretched P/E, above band, below band), new-results review, R7 band, R18/R21 gates, policy bounds, weight-based sizing with no stop orders, rebalance orders, backtest determinism and fundamentals sensitivity.
- Integration on PostgreSQL (`test_phase6_api.py`, 4): fundamentals sync/listing (no rewrite, unpublished quarter hidden), entry blocked without fundamentals, full lifecycle (entry → fill → new-results review with valuation history; clean reconciliation and audit chain), long-term backtest API.
- Web (`status.test.ts`): long-term rule descriptions and band position.

## Phase 5 coverage

- **Swing strategy:** entry with thesis, insufficient history, no trend, event proximity, the three exit reasons, holding with no reason.
- **Look-ahead:** a future announcement and the daily bar end.
- **Event source:** mock events are deterministic and announced ahead; point-in-time queries.
- **Gates and policy:** R18 (unsynced, stale, blackout boundary, none known, long-term), R3 against evaluation time, delivery cost rates, event-policy bounds.
- **SwingRunner:** multi-day lifecycle (DELIVERY orders, fill at the next open, protection kept across sessions), R18 blocking without coverage, unfilled-entry expiry, rejection of intraday bars, tick snapping, ledger-scoped P&L.
- **Swing backtests:** determinism, event sensitivity, prefix invariance, interval/horizon check.
- **Integration:**
  - event sync, listing and manual entry;
  - R18 before a sync;
  - full multi-session thesis lifecycle with reconciliation and the append-only trigger;
  - swing research cycle creating no orders or theses;
  - swing backtest API with splits, and without an event source.

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
