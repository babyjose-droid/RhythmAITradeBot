# Backtesting Methodology

**Status:** Implemented in Phase 3 for the intraday baseline (`src/rhythm/backtest/`). Items below marked *(planned)* are not built yet.

**Swing (Phase 5).**
- **Bars and runner:** daily bars through `SwingRunner`. A bar's decision time is its session close, and fills happen at the next open.
- **Events:** taken point-in-time via `known_at`. Instruments with no event source get no swing entries (R18 fails closed).
- **Run identity:** the run hash includes an events digest.
- **Warm-up:** the first 51 sessions of any run or split are warm-up only (SMA50).

**As built (Phase 3):** bars replay chronologically through `IntradayRunner` — the same code paper trading uses. `StrategyContext` refuses future or foreign bars. Orders fill at the next bar's open with 5 bps adverse slippage, clamped to the bar's high–low range and capped at 10 % of bar volume. A stop that gaps fills at the open. Within a bar the order is stop > exit > entry > target. Costs come from the versioned cost model (`illustrative-unverified-2026-10`, **unverified**). Reproducibility: `data_hash` (bars) + `run_hash` (strategy, version, params hash, cost model, fill and risk settings, data hash). Results are stored in `backtest_runs` and `backtest_trades`. Metrics: total return, max drawdown, volatility, Sharpe (rf = 0, labelled), trades, win rate, expectancy, profit factor, average R, charges, turnover, exposure, equal-weight buy-and-hold benchmark, exit reasons. Each is reported for the full period and for each named split. Runs on MOCK data are labelled `data_is_mock`. Not yet built: survivorship flag (no point-in-time universe source), corporate-action adjustment, circuit limits, walk-forward automation.

Design (Phase 0):

- **Event-driven, chronological**: a single clock replays bars/quotes; strategies see only data with `ts ≤ now`; fills occur no earlier than the next event after the decision.
- **Same code paths** as paper trading: strategy → orchestrator → risk engine → paper fill model.
- **No survivorship bias**: point-in-time universes and instrument snapshots (B-07 — requires archived scrip masters / vendor history). Backtests on data lacking point-in-time universes are flagged `SURVIVORSHIP_RISK` in results.
- **Costs**: brokerage per the owner's Sharekhan plan (input), STT, exchange transaction charges, SEBI turnover fee, stamp duty, GST, DP charges — rates held in a versioned `cost_model` with sources and effective dates (to be filled from primary sources in Phase 3, not assumed).
- **Slippage & liquidity**: spread/half-spread + volume-participation model; partial fills when order > x% of bar volume; no fills outside bar high/low; circuit limits respected.
- **Corporate actions**: Sharekhan history is unadjusted (FAQ). Adjust when a corporate-actions source exists; otherwise mark affected windows.
- **Splits**: development / validation / out-of-sample periods fixed before tuning; walk-forward evaluation; parameter search limited and logged.
- **Metrics**: total & annualised return, max drawdown, volatility, Sharpe/Sortino (labelled), win rate, expectancy, avg R, turnover, exposure, costs paid, benchmark comparison (e.g. NIFTY 50 TRI if licensed data available).
- **Reproducibility**: run ID binds strategy version, data snapshot hash, cost-model version, seed, code commit.
- **Reporting rule**: never rank by headline return alone; always show drawdown, costs and out-of-sample results side by side.
- **Data constraint**: with only 7 days of Sharekhan intraday history (B-06), meaningful intraday backtests need a licensed vendor or locally accumulated data.
