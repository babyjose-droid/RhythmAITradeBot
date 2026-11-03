# Phase 6 Report — Long-Term Fundamentals, Theses, Valuation History, Allocation Bands

**Date:** 2026-11-03 · **Status:** complete, awaiting owner review before Phase 7.
**Data used:** MOCK only. Licensed fundamentals (decision D-03, blocker B-06) do not exist yet, so every fundamental number in this phase is synthetic and labelled `source=MOCK`. No Sharekhan endpoint was called, no credentials were used and no real order exists anywhere.

## Scope and acceptance criteria

| Criterion (blueprint Phase 6) | Delivered |
|---|---|
| Fundamentals evidence model | `FundamentalSnapshot` per quarter: revenue, net income, EPS, book value/share, operating cash flow, total debt, total equity; every snapshot carries `period_end`, `known_at` (publication time), `source` and `source_ref`, and each metric has provenance. Derived metrics (`eps_ttm`, `ocf_ttm`, `roe`, `debt_to_equity`, `pe`, `pb`, `eps_growth_yoy`) carry their **formula and inputs** |
| Point-in-time correctness | `known_as_of()` returns only snapshots published by the decision time, and the latest restatement per period wins. `StrategyContext` raises `LookAheadError` on unpublished fundamentals. Rows in `fundamental_snapshots` are never rewritten (a restatement is a new row) |
| MOCK fundamentals source | `MockFundamentalsProvider`: deterministic (fixed anchor), 45-day publication lag, profiles per symbol so the screen has passes and failures (MOCKE passes; MOCKA fails ROE; MOCKF fails valuation). Refused in staging/production |
| Long-term baseline strategy | `long_term_quality_value@1.0.0` — quality (ROE ≥ 15 %, D/E ≤ 1, TTM OCF > 0, EPS growth ≥ 0) at a reasonable price (P/E ≤ 30), fresh data (≤ 200 days), no known event within 5 days. Target weight 10 % of the ledger, band 5–15 % |
| Thesis tracking | Entry thesis stores rationale, invalidation rules (`roe_below`, `ocf_ttm_not_positive`, `eps_growth_below`), allocation band, review interval, key metrics with formulas and evidence provenance. An invalidated thesis exits the whole position |
| Review schedule & valuation history | Review every 20 held sessions **and** whenever new results are published. Each review is an append-only OPEN→OPEN thesis event with the metrics, weight, band, latest period and whether it was triggered by new results — this is the valuation history |
| Allocation bands | On review: P/E > 45 → REDUCE to 5 %; weight > 15 % → REDUCE to 10 %; weight < 5 % with the thesis intact → ADD to 10 %. Signals carry `target_weight`; the runner converts it into a share quantity. No stop/target orders for long-term positions |
| Risk engine | `risk-engine-1.2`, policy `paper-default-3`: **R7** for LONG_TERM caps the position at `long_term_max_weight` (15 %) of the ledger allocation (fail-closed when unknown); **R18** event gate now applies to LONG_TERM too; new **R21** blocks long-term entries without published fundamentals or with fundamentals older than `max_fundamentals_age_days` (200). REDUCE/EXIT intents skip the gating checks |
| PAPER & backtest | `run_cycle` routes LONG_TERM to the daily runner; backtests for LONG_TERM receive point-in-time fundamentals and events; the run hash includes a fundamentals digest; new metric `positions_open_at_end`. Cycle responses include per-instrument **diagnostics** (why / why not) |
| Dashboard | New **Long-term** page: evidence table with formula tooltips, cycle why/why-not, theses with an allocation-band visual, review/valuation history, long-term orders and holdings. Backtests page offers the strategy |

**New endpoints:** `POST /v1/fundamentals/sync` (503 without a provider), `GET /v1/fundamentals/{exchange}/{symbol}?quarters=` (quarters + derived metrics with formulas, `is_mock`); `GET /v1/theses` gains `horizon=`.
**Migration:** `0006_fundamentals` (table `fundamental_snapshots`, unique on exchange/symbol/period_end/source/known_at). Upgrade, downgrade and re-upgrade ran clean.
**Config:** `RHYTHM_FUNDAMENTALS_PROVIDER=none|mock` (`mock` refused in staging/production); `demo.env` sets `mock`.
**CLI:** `rhythm-cli backtest-run --strategy long_term_quality_value`. The demo script now syncs fundamentals and runs a long-term cycle.

## Commands run and results (exact)

Cloud session, Python 3.13 and 3.12.3, PostgreSQL 16, Node 22:

```
$ ruff check src tests scripts          → All checks passed!
$ ruff format --check src tests scripts → 142 files already formatted
$ mypy (strict)                         → Success: no issues found in 141 source files
$ lint-imports                          → Contracts: 11 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1)  → 355 passed, 1 warning; coverage 94 %
$ pytest on Python 3.12.3               → 355 passed, 1 warning
$ pip-audit --skip-editable             → No known vulnerabilities found
$ web: tsc --noEmit                     → ok
$ web: vitest run                       → Tests 7 passed (7)
$ web: next build                       → Compiled successfully (route /long-term built)
$ web: npm audit --omit=dev             → found 0 vulnerabilities
```
New tests: 29 unit (`test_phase6_core.py`) + 4 integration on PostgreSQL (`test_phase6_api.py`). The warning is Starlette's TestClient deprecation notice. New import contract: fundamentals types and the MOCK source are pure.

**Demo run (real API + PostgreSQL, MOCK data):** long-term cycle → 1 PAPER order for MOCKE; MOCKF "entry conditions not met" (`valuation_ok: false`); MOCKA "entry conditions not met" (`quality: false`). Long-term backtest over 500 days: HTTP 201, 0 closed trades (positions are held, not round-tripped).

**Not run this phase:** a Playwright UI smoke test of the Long-term page (it builds and type-checks; visual check pending).

**Defects found and fixed during the build:**
- MOCK fundamentals depended on the query window (not reproducible) → fixed anchor date;
- an integration scenario got no entry because a MOCK results date was 2 days away (R18 + strategy event filter, working as designed) → scenario date moved, and cycle **diagnostics** added so such cases are visible;
- older tests pinned the policy version and check count → now read `DEFAULT_POLICY.version` and 20 checks.

## What the results do and do not mean

The screen, thesis and review machinery is exercised end to end, but on **synthetic** fundamentals. Nothing here says anything about real companies or real returns. Real use needs licensed, point-in-time fundamentals with restatement history (B-06/D-03) and corporate-action adjustments.

## Limitations carried forward

See KNOWN_LIMITATIONS.md: MOCK fundamentals only; no corporate-action adjustment; no sector data (R11 skipped); P/E uses the last close and TTM EPS without adjustments; no cost-basis tax lots.

## Next: Phase 7 (awaiting approval)

Claude research service: frozen evidence packets, schema-validated assessments that may only downgrade or flag rule-generated candidates, prompt versioning, token/cost budgets — never order submission.
