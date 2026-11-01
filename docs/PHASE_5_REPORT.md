# Phase 5 Report — Swing Strategy, Event Gate, Thesis Tracking, Multi-Day PAPER Workflow

**Date:** 2026-11-01 · **Status:** complete, awaiting owner review before Phase 6.
**Data used:** MOCK only (synthetic prices and synthetic results dates). No Sharekhan endpoint was called, no credentials were used, and no real order exists anywhere.

## Scope and acceptance criteria

| Criterion (blueprint Phase 5) | Delivered |
|---|---|
| Swing strategy | **`swing_trend_pullback@1.0.0`**, a pure function of daily bars plus known events. Long only.<br>• **Trend:** close > SMA50 and SMA20 > SMA50.<br>• **Entry:** a close back above SMA20 after a pullback to or below it within 5 sessions.<br>• **Stop / target:** stop = 10-session low, required to be 1–8 % away; target = 2R.<br>• **No entry** within 5 days of a known event.<br>• **Exits:** close below SMA50 (thesis invalidated), a 20-session time stop, or a known event within 1 day. Stop and target are protective orders kept at the venue across sessions |
| Overnight / event gate (R18) | Point-in-time corporate-event calendar: every event records **when it became known**, and only events public at decision time are used. A strategy context containing a not-yet-announced event raises `LookAheadError` (tested).<br>R18 blocks a swing entry when:<br>• the instrument's calendar was never synced;<br>• it was last synced more than 7 days ago;<br>• a known event is ≤ 3 days away.<br>Manual events add information but never count as coverage. Exits are never blocked by R18. **Fail closed:** a swing backtest with no event source opens nothing (tested on the API and in unit tests) |
| Thesis invalidation | Each swing entry stores its thesis: rationale, invalidation rules (SMA50, stop price, maximum holding period, event proximity) and review cadence.<br>• The thesis is checked **every session**, and the latest diagnostics are kept (e.g. held sessions, days to the next event, exit reasons).<br>• Status flow: `PENDING_ENTRY → OPEN → INVALIDATED → CLOSED`.<br>• Every change goes to append-only `thesis_events`, together with the evidence (exit order id, reasons).<br>• The thesis closes only when the position is actually flat, with the closing order's purpose recorded (stop / target / exit) |
| PAPER workflow, multi-day | The same cycle endpoint runs swing when `strategy: swing_trend_pullback`. **Run it after the close.**<br>• The first cycle uses history only as warm-up and decides on the latest completed session. Later cycles process every unseen session; only the latest session, with a FRESH daily bar, may open positions.<br>• Orders are DELIVERY product and fill at the next session's open. Entry quantity left unfilled after that session expires.<br>• Fills are capped at 10 % of the session's volume. Any exit remainder is re-sent on later sessions until the position is flat (seen in the end-to-end test).<br>• Each cycle runs the full Phase 4 machinery: pre/post reconciliation, the kill switch (now also cancels swing entries), pauses, UNKNOWN resolution, risk persistence and audit |
| Backtests | One engine for both horizons: the strategy's horizon selects `IntradayRunner` (5-minute) or `SwingRunner` (daily). Swing backtests take events (the run hash includes an events digest).<br>Tests cover:<br>• deterministic and event-sensitive results;<br>• prefix invariance on daily data;<br>• the interval must match the horizon.<br>Duplicate symbols are now rejected; this was a bug found by a test. `rhythm-cli backtest-run --strategy swing_trend_pullback` works on MOCK data |
| Multi-horizon correctness | **Ledger-scoped daily P&L for R8.** P&L = today's cash flow from *this ledger's* fills + the change in the value of its positions. A swing purchase therefore can no longer look like an intraday loss (tested). The per-ledger cycle state is namespaced (`INTRADAY` / `SWING`); the Phase 3/4 flat format is still read.<br>**Tick snapping.** Strategy prices are snapped to the instrument tick: stop rounds towards price, target rounds down |
| Dashboard | New **Swing workspace**:<br>• event calendar with per-instrument coverage ("never — entries blocked");<br>• theses with status, rationale, invalidation rules in words, last check (exit reasons, held sessions) and next review;<br>• swing candidates, orders and positions.<br>The Backtests page has a strategy selector. The Intraday workspace now filters to the INTRADAY ledger |

**New endpoints:**
- `POST /v1/events/sync` — from the configured provider: `RHYTHM_EVENT_PROVIDER=mock` (local/test only).
- `POST /v1/events` — manual, with a source reference; `known_at` cannot be in the future.
- `GET /v1/events?watchlist_id=` — events plus last sync per instrument.
- `GET /v1/theses?account_id=&status=`, `GET /v1/theses/{id}` (with history).

**Changed endpoints:**
- `GET /v1/paper/accounts/{id}/orders` and `GET /v1/signals` accept `horizon`.
- Positions output includes `horizon`.
- The cycle accepts swing strategies.
- `POST /v1/backtests` picks the bar interval from the strategy.

**Migration `0005_events_theses`:**
- `corporate_events` — unique per instrument/kind/date/source; `known_at` stored.
- `event_syncs`.
- `theses`.
- `thesis_events` — append-only trigger.

Upgrade, downgrade, re-upgrade and the model drift check all ran clean.

**Risk engine:** now `risk-engine-1.1`, with policy `paper-default-2`. It adds `event_blackout_days=3` and `event_calendar_max_age_days=7`. R3 now measures reconciliation age against the cycle's wall-clock time. That matters for swing, where the decision refers to the last close.

**Import contracts:** 10 kept. New: event types and the mock event source are pure.

## Commands run and results (exact)

Cloud session, Python 3.13.18 and 3.12.3, PostgreSQL 16, Node 22:

```
$ ruff check src tests            → All checks passed!
$ ruff format --check src tests   → 132 files already formatted
$ mypy (strict)                   → Success: no issues found in 132 source files
$ lint-imports                    → Contracts: 10 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1) → 322 passed, 1 warning; coverage 94 %
    risk/ + orders/ + portfolio/      → 98 %
    swing strategy / events / theses / runners → 95 %
$ pytest on Python 3.12.3              → 322 passed, 1 warning
$ pip-audit --skip-editable            → No known vulnerabilities found
$ web: tsc --noEmit                    → ok
$ web: vitest run                      → Tests 6 passed (6)
$ web: next build                      → Compiled successfully (7 routes)
$ web: npm audit --omit=dev            → found 0 vulnerabilities
```
The warning is Starlette's third-party TestClient deprecation notice.

**End-to-end test** (`test_swing_multi_day_paper_lifecycle_with_thesis`, PostgreSQL, fixed clock, MOCKB), session by session:
- **Before any sync:** R18 blocks the entry.
- **After sync, day 0:** an entry order is placed and the thesis is `PENDING_ENTRY`. R18 shows PASS in the stored decision.
- **Day 1:** the entry fills at the open and stop/target are placed. At the close, price falls below SMA50, so the thesis becomes `INVALIDATED`, protection is cancelled and an exit order is queued.
- **Following sessions:** the exit fills. A thin session leaves a remainder, which is re-sent until the position is flat.
- **Close:** the thesis is `CLOSED — position closed by exit`. The history is exactly `PENDING_ENTRY, OPEN, INVALIDATED, CLOSED`, total sold = total bought, and every reconciliation was OK. The audit chain verifies, and `thesis_events` rejects UPDATE.

**UI smoke test.** Playwright with Chromium ran against the real API and PostgreSQL, two sessions:
- the Swing workspace showed "never — entries blocked" before the sync;
- *Sync event calendar*, then *Run PAPER swing cycle*, gave one order and a `PENDING_ENTRY` thesis;
- after the next session: MOCKB `INVALIDATED` with "thesis invalidated: close 1192.80 below SMA50" shown in red, plus a new MOCKA entry;
- a MOCK results date 7 days away is listed in the calendar.

**Defects found and fixed during the phase:**
- Daily bars "ended" 24 h after the open, which made every swing decision look like look-ahead. The runner now supplies the session close as the bar end.
- R3 rejected swing decisions taken hours after the close; it now uses the cycle's wall-clock time.
- An entry filled and stopped out in the same session left the thesis stuck in `PENDING_ENTRY`; it now goes OPEN then CLOSED.
- An exit signal was not recorded as an invalidation when the entry filled in the same cycle; the thesis update order was fixed.
- Duplicate backtest symbols corrupted history; they are now rejected.
- Non-tick strategy prices were failing R6; they are now snapped.

## Results — what they do and do not mean

Swing backtests on MOCK data trade often, because the synthetic daily walk crosses its moving averages often and has wide daily ranges. The results validate the pipeline (gating, holding, exits, events, costs). They say **nothing** about edge on real stocks. In the UI run, one entry filled about 5 % below its decision price because of a synthetic gap. Gap risk on real data is real, and position sizing does not yet account for it (see limitations).

## Design decisions

- **Decision time = the session close; fills at the next open.** This keeps swing look-ahead-free and matches an after-close workflow.
- **R18 coverage needs a provider sync.** A manually typed event cannot prove there are no other events.
- **Theses close on facts, not intentions.** INVALIDATED means an exit is queued; CLOSED only when the ledger is flat.
- **R8 is ledger-scoped.** The daily loss stop measures each ledger's own P&L, so horizons don't trip each other through shared cash.

## Limitations carried forward

See KNOWN_LIMITATIONS.md:
- the event calendar is MOCK only (a real source is needed — B-06);
- no gap-risk sizing (a gap beyond the stop can exceed the planned loss);
- MOCK daily volume is erratic, causing partial exits;
- swing positions are not adjusted for corporate actions;
- the swing daily-loss baseline is per decision day;
- the cost model is unverified;
- long-term (Phase 6) has no event or allocation policy yet, so R18 still blocks it.

## Next: Phase 6 (awaiting approval)

- Long-term fundamentals evidence model (licensed data needed, D-03; otherwise MOCK-labelled);
- thesis and valuation tracking with review schedules;
- allocation bands;
- LONG_TERM ledger workflow.
