# Strategy Specification

**Status:** Phase 0 outline. Concrete rules and parameters are fixed per strategy version in its phase, before any backtest is run (to limit overfitting). Nothing here is a claim of profitability.

## 1. Common contract

- Pure function of a point-in-time `StrategyContext` (bars, quotes, instrument metadata, own ledger positions); returns `Signal`s with `entry`, `stop`/invalidation, `target` (optional), `time_stop`, `rationale_codes`.
- Versioned (`strategy_versions`), parameters frozen per version; any change = new version.
- No I/O, no randomness without seeded RNG, no use of data timestamped after the decision time.

## 2. Intraday baseline (Phase 3) — candidate: Opening-Range Breakout with VWAP filter

- Universe: liquid NC equities from approved universe. Bars: 5-minute.
- Long setup: price closes above opening-range high (first N minutes) **and** above session VWAP, volume above rolling median; stop = opening-range low or ATR-based, whichever tighter within bounds; one trade per symbol per day.
- Short side: **disabled** in MVP pending product-type clarification (B-02).
- Exits: stop, target (R-multiple), VWAP loss, end-of-session policy.
- Chosen because it is simple, explainable and testable — a baseline, not an edge claim.

## 3. Swing (Phase 5) — implemented: `swing_trend_pullback@1.0.0`

**Bars and timing.** Daily bars. The decision is taken at the session close, and fills happen at the next open.

**Entry rules:**
- **Trend:** close > SMA50 and SMA20 > SMA50.
- **Trigger:** a close back above SMA20 after a close at or below it within the previous 5 sessions.
- **Stop:** the 10-session low, required to be 1–8 % below the entry.
- **Target:** 2R.
- **Event filter:** no entry within 5 days of a known event. The risk engine's R18 also blocks within 3 days, or without a recent calendar sync.

**Thesis.** Stored with the entry: rationale, plus invalidation rules (`close_below_sma 50`, `stop`, `max_holding_sessions 20`, `exit_before_event_days 1`).

**Exits:**
- close below SMA50 (thesis invalidated);
- time stop;
- a known event within 1 day;
- stop/target as protective orders kept across sessions.

**Not implemented:** gap-risk sizing (see KNOWN_LIMITATIONS), 200-day filter and breakout variant (a possible v2). Parameters are frozen for v1.0.0.

## 4. Long-term (Phase 6) — evidence-based thesis tracking

Quality/valuation screens from licensed fundamentals (B-06); output is a thesis with allocation band, key metrics with provenance, review date; actions ACCUMULATE/HOLD/REDUCE map to ADD/HOLD/REDUCE candidates. Without licensed fundamentals the module runs on mock data clearly labelled MOCK.

## 5. Claude's role

Assessment of a rule-generated candidate using the evidence packet only (see MULTI_HORIZON_PORTFOLIO.md §4 and API_SPECIFICATION.md research schema). Can downgrade or flag; cannot create or upsize.
