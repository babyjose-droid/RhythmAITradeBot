# Phase 3 Report — Intraday Strategy, Backtester, Paper Trading, First Dashboard

**Date:** 2026-10-31 · **Status:** complete, awaiting owner review before Phase 4.
**Data used:** MOCK only. No Sharekhan endpoint was called. No credentials were used. No real order exists anywhere in the system.

## Scope and acceptance criteria

| Criterion (blueprint Phase 3) | Delivered |
|---|---|
| Intraday baseline strategy | `intraday_orb_vwap@1.0.0`: 15-min opening range. Long entry 09:30–14:45 IST when close > OR high, close > session VWAP and volume ≥ 1× median of prior bars. Stop = OR low, rejected if the stop is more than 2 % away. Target = 2R. Exits on a VWAP break or at 15:05; at most 1 entry per symbol per day. Pure functions; parameters hashed into the run id |
| No look-ahead | `StrategyContext` raises `LookAheadError` if any bar ends after `now`, belongs to another instrument or is out of order. Tests: **prefix invariance** (trades before T are identical whether or not later data exists) and **future tampering** (changing later bars never changes earlier trades) |
| Event-driven backtester | Bars are replayed chronologically through the **same** `IntradayRunner` the paper service uses: strategy → baseline risk gate → paper book → fill model. Fills happen no earlier than the next bar's open. Splits are named and must not overlap. `data_hash` and `run_hash` make runs reproducible (same inputs give the same hash and the same trades — tested) |
| Cost model | Versioned `illustrative-unverified-2026-10`, `verified=false`: brokerage (placeholder rates), STT, exchange transaction charge, SEBI fee, stamp duty, GST. Every run stores the model's description. **Rates are illustrative and must be replaced from a current Sharekhan contract note before results mean anything** |
| Fill model | Market orders fill at the next open ± 5 bps adverse slippage, clamped to the bar range. Limit orders fill only when marketable. Stops fill at min(open, stop) on a gap down. Volume participation is capped at 10 % of bar volume, so large orders fill partially. Long-only: a sell larger than the position is refused |
| Paper order lifecycle | 15-state machine with an explicit transition table (illegal moves raise). Idempotency key per signal (`strategy:version:instrument:time:type`). Protective stop and target are linked as OCO. Within a bar, priority is stop > exit/square-off > entry > target (conservative). Session-close square-off; working orders expire. Order events and fills are **append-only** (DB triggers) |
| PAPER isolation | `orders.account_kind` and `fills.account_kind` have CHECK constraints = 'PAPER'. All PAPER API responses carry the `x-account-kind: PAPER` header. Research cycles use a throwaway book and never write orders |
| Baseline risk gate | `baseline-0.1` (interim until Phase 4): 0.5 % equity risk per trade, 20 % position cap, cash buffer, 1.5 % daily loss stop, maximum open orders and positions, no pyramiding, no martingale. Stale data blocks entries; exits are always allowed |
| Catch-up safety | A paper cycle replays bars missed since the last cycle, but **only the latest bar with a FRESH quote may open positions**. Missed bars can only exit. Tested |
| First dashboard screens | Next.js 16 app (`web/`): Overview, Intraday workspace, Backtests. Persistent **PAPER / RESEARCH** mode banner. Candidate, approved, at-broker, partial and filled states each use a different label, colour **and** border style (works without colour). MOCK DATA and "costs unverified" tags. Freshness pills fail closed |

**New endpoints:**
- `GET /v1/strategies`
- `POST|GET /v1/backtests`, `GET /v1/backtests/{id}`
- `POST|GET /v1/paper/accounts`, `GET /v1/paper/accounts/{id}` and its `/orders`, `/fills`; `POST /v1/paper/accounts/{id}/cycle`
- `GET /v1/paper/orders/{id}/events`, `POST /v1/paper/orders/{id}/cancel`
- `POST /v1/research/cycle`
- `GET /v1/signals`

**New CLI:** `rhythm-cli backtest-run` (mock), `rhythm-cli paper-account-create`.

**Migration** `0003_paper_backtests` adds `paper_accounts`, `orders`, `order_events`, `fills`, `paper_positions`, `strategy_signals`, `backtest_runs`, `backtest_trades`, plus append-only triggers.

**Docker:** a new `web` service in Compose (`127.0.0.1:3000`), and CI publishes a second image `ghcr.io/<repo>-web`.

## Commands run and results (exact)

Cloud session, Python 3.13.18 and 3.12.3, PostgreSQL 16, Node 22:

```
$ ruff check src tests            → All checks passed!
$ ruff format --check src tests   → 105 files already formatted
$ mypy (strict)                   → Success: no issues found in 105 source files
$ lint-imports                    → Contracts: 7 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1) → 193 passed, 1 warning; coverage 95 %
$ pytest on Python 3.12.3              → 193 passed, 1 warning
$ pip-audit --skip-editable            → No known vulnerabilities found
$ web: tsc --noEmit                    → ok
$ web: vitest run                      → Tests 4 passed (4)
$ web: next build                      → compiled successfully
$ web: npm audit --omit=dev            → found 0 vulnerabilities
```
The warning is Starlette's third-party TestClient deprecation notice. The three new import contracts are:
- strategies are pure;
- risk, execution and orders cannot reach the API, DB, broker or paper modules;
- the backtester cannot reach the API, broker or paper modules.

**UI smoke test** (Playwright with Chromium, against the real API and PostgreSQL, fixed clock inside a session):
- PAPER cycle 1 created 1 order, shown as **AT BROKER — not filled**;
- cycle 2 produced 1 simulated fill, shown as **FILLED**;
- badge kinds seen on the page: candidate, at_broker, filled, closed;
- the banner read "PAPER TRADING — simulated orders and fills only";
- the Backtests page showed the MOCK DATA and "costs unverified" tags.

**Defects found during the build and fixed:**
- a same-bar stop-out recorded `exit_at == entry_at` (the test now allows ≥);
- `/v1/strategies` crashed on a strategy without a docstring;
- VWAP was computed in float (now Decimal);
- Next 15 bundled a postcss version with a high-severity advisory — upgraded to Next 16.4.3.

## Results — what they do and do not mean

A 30-day backtest on **MOCK random-walk data** loses money after costs. That is the expected result and confirms costs are applied. It says **nothing** about the strategy's real-world edge. Meaningful results need licensed intraday history (B-06), a verified cost model and out-of-sample splits fixed before any tuning.

## Design decisions

- One runner for backtests and paper trading, so behaviour cannot drift between the two.
- Same-bar ambiguity is resolved against us: stop before target, and an entry filled on a bar is checked against its stop on that same bar.
- The risk gate is deliberately minimal and versioned; Phase 4 replaces it with the full risk engine without changing the runner interface.
- Candidates (`strategy_signals`) are stored separately from orders and linked by `order_id`, so the UI can show that an idea is not an order.

## Limitations carried forward

See KNOWN_LIMITATIONS.md. In short:
- unverified cost model;
- interim risk gate;
- MOCK-only data;
- long-only;
- no authentication (loopback only);
- the dashboard's API address is baked in at build time.

## Next: Phase 4 (awaiting approval)

- Full risk engine (every check unit-tested, fail-closed);
- per-horizon ledgers with a consolidated view;
- kill switch and strategy pauses;
- idempotency/UNKNOWN-state handling;
- reconciliation.
