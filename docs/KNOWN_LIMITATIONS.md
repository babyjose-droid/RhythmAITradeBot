# Known Limitations & Blockers

**Status:** Phase 0 (2026-10-31). Blockers stop a specific capability, not the whole project — Phases 1–4 can proceed on mocks and paper.

## 1. Blockers

| ID | Blocker | Impact | Unblocks with |
|---|---|---|---|
| B-01 | SEBI/exchange retail-algo requirements (static IP, algo ID/registration, tagging) not verified; Sharekhan docs silent | Any live order path; hosting design | Sharekhan written confirmation + primary circulars |
| B-02 | Cash-equity `productType` for intraday vs delivery unclear (`INVESTMENT/BIGTRADE/BIGTRADE+` vs `CNC/MIS` in samples); auto square-off time unknown | Intraday ledger semantics, end-of-session policy, cross-horizon netting | Sharekhan confirmation |
| B-03 | Rate limits: "up to 30 req/s" (marketing) vs FAQ placeholder "XX"; data-endpoint limits unknown | Throttling config | Sharekhan confirmation; conservative defaults meanwhile |
| B-04 | Redirect-URL registration for the login flow undocumented | Real login (Phase 2 read-only verification) | Sharekhan / API portal |
| B-05 | "Available limit" must be computed client-side; formula undocumented | Funds check R13 for live | Sharekhan confirmation; paper uses own cash ledger |
| B-06 | Sharekhan intraday history = last 7 days only; no fundamentals, corporate actions, news or historical constituents | Intraday/swing backtests, long-term research | Licensed data vendor(s) chosen by owner (D-03) |
| B-07 | Scrip master = current-day active scrips only | Survivorship bias in backtests | Start archiving daily snapshots immediately once login works; vendor history |
| B-08 | Endpoint discrepancies D1–D8 between docs and SDK | Order-trade and history adapters | Observed responses (read-only) + Sharekhan |
| B-09 | No sandbox/UAT environment documented | All broker integration testing is mock-only; any real call is production | Accept; read-only supervised checks only |
| B-10 | No client order ID / idempotency field | Duplicate-safety for live | Internal idempotency + reconciliation (designed) |
| B-11 | ~~No repository~~ **Resolved 2026-10-31:** code in `~/Projects/rhythm-ai-trader` and github.com/babyjose-droid/RhythmAITradeBot | — | — |

## 2. Known limitations (by design or data)

- **Phase 2:** no exchange holiday list is bundled; until one is loaded from the exchange's published calendar, holidays count as trading days (freshness then reports STALE — safe). Special/muhurat sessions are not modelled.
- **Phase 2:** which Sharekhan `instType` codes denote cash equity is unverified (docs say "EQ, FS, FI"; the sample row shows "SM"); the parser requires an explicit allow-list (default `EQ`).
- **Phase 2:** the only working data sources are MOCK. Real instruments and prices need Sharekhan read-only verification (approval required) and/or a licensed vendor (B-06).
- **Phase 2:** the API has no authentication; it binds to loopback only. Watchlist edits are audited as `local-owner`.

- Historical candles are **not corporate-action adjusted** (FAQ); adjustments need a corporate-actions source.
- Access token lasts 24 h with no documented refresh: **daily manual login** is required; unattended overnight operation will drop to RESEARCH_ONLY when the token expires.
- WebSocket: max 1000 symbols/connection; reconnect/gap semantics undocumented — the freshness monitor must assume gaps.
- No market-data timestamp timezone stated; assumed IST, to verify.
- Fees/slippage models are estimates; paper results will differ from live.
- Backtests cannot represent queue position, partial-fill microstructure or impact precisely.
- LLM assessments can be wrong or inconsistent; they are advisory and logged with prompt version.
- MVP scope: Indian cash equities only (no F&O, leverage, other brokers, international).
- This research was done without web search (disabled for this organisation); regulatory content relies on direct page fetches only.
