# Backtesting Methodology

**Status:** Phase 0 outline; implemented in Phase 3.

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
