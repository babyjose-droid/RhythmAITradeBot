# Phase 2 Report — Data and Watchlists

**Date:** 2026-10-31 · **Status:** complete, awaiting owner review before Phase 3.
**Data used:** MOCK only. No Sharekhan endpoint was called. No credentials were used.

## Scope and acceptance criteria

| Criterion (blueprint Phase 2) | Delivered |
|---|---|
| Instrument management | Point-in-time registry (`instruments` versions with `valid_from`/`valid_to`), scrip-master snapshots (`instrument_snapshots`, raw rows kept), add/change/delist detection, idempotent re-apply, fail-closed rejection of empty, invalid, out-of-order, same-day-conflicting and mass-removal (>20 %) snapshots — each rejection stored and raised as a `SNAPSHOT_REJECTED` data-quality event |
| Watchlist management | CRUD + items keyed by `EXCHANGE:SYMBOL` (survives instrument version changes), item must be active in the registry, every mutation audited in the hash chain |
| Market-data abstraction | `MarketDataProvider` protocol; `Quote`/`Bar` types with `source`, `source_ts`, `received_ts`; Decimal prices; OHLC/quote validation; idempotent bar storage with provenance; MOCK refused in staging/production (settings, registry, bar store and CLI) |
| Source timestamps | Every quote/bar/instrument carries origin and timestamps; API responses expose `source` and `is_mock`; stored-bar freshness reports `db:<origin>` |
| Stale-data detection | Session calendar (NSE/BSE 09:15–15:30 IST, Mon–Fri); per-horizon rules — intraday quotes FRESH ≤ 10 s in session (configurable), SESSION_CLOSED after hours, STALE/MISSING/INVALID/ERROR otherwise; swing/long-term need the daily bar of the last completed session. Only `FRESH` is marked `usable_for_new_orders`. Monitor records one event per transition (and `RECOVERED`), not per poll |
| Mocked-provider tests | Deterministic `MockMarketDataProvider` and `MockBrokerAdapter` with failure injection (stale, missing, error, outage, future timestamp, crossed market, auth-expired, rate-limited, unavailable) |
| Broker interface (planned in Phase 0) | Read-only `BrokerReadAdapter` (profile, funds, holdings, positions, day orders, scrip master) with **no order methods** (tested). Sharekhan scrip-master/envelope parsers written against documented shapes only, marked unverified |

New endpoints: `GET /v1/instruments`, `GET /v1/instruments/{exchange}/{symbol}`, `GET /v1/instruments/snapshots`, `POST /v1/instruments/sync`, `GET|POST /v1/watchlists`, `GET|DELETE /v1/watchlists/{id}`, `POST /v1/watchlists/{id}/items`, `DELETE /v1/watchlists/{id}/items/{exchange}/{symbol}`, `GET /v1/marketdata/quote/{exchange}/{symbol}`, `GET /v1/marketdata/bars/{exchange}/{symbol}`, `POST /v1/marketdata/bars/{exchange}/{symbol}/backfill`, `GET /v1/marketdata/freshness`, `GET /v1/data-quality/events`.
New CLI: `rhythm-cli instruments-sync|bars-backfill|watchlist-seed-mock` (mock source only).
New settings: `RHYTHM_MARKET_DATA_PROVIDER`, `RHYTHM_BROKER_ADAPTER` (`none` default, `mock` local/test only), `RHYTHM_FRESHNESS_INTRADAY_MAX_AGE_SECONDS`, `RHYTHM_LOCAL_ACTOR_ID`.
Migration: `0002_instruments_marketdata` (instrument_snapshots, instruments, watchlists, watchlist_items, market_bars, data_quality_events).

## Commands run and results (exact)

Cloud session, Python 3.13.18 and 3.12.3, PostgreSQL 16:

```
$ ruff check src tests            → All checks passed!
$ ruff format --check src tests   → 81 files already formatted
$ mypy (strict)                   → Success: no issues found in 81 source files
$ lint-imports                    → Contracts: 4 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1) → 153 passed, 1 warning; coverage 96 %
$ pytest on Python 3.12.3              → 153 passed, 1 warning
$ pip-audit --skip-editable            → No known vulnerabilities found
```
The warning is Starlette's third-party TestClient deprecation notice.

**Smoke test** (real processes against Postgres): `alembic upgrade head` → `0002_instruments_marketdata (head)`; `rhythm-cli instruments-sync --source mock` → 20 added; `watchlist-seed-mock --count 3`; `bars-backfill` → 21 daily bars written, 0 rejected; API with mock providers: `/health/ready` 200; quote labelled `MOCK` with `SESSION_CLOSED` on a Saturday; swing freshness FRESH for the backfilled name and MISSING for the others, with two `MISSING` quality events recorded; `/v1/audit/verify` ok (6 rows). Production + `RHYTHM_MARKET_DATA_PROVIDER=mock` → app refuses to start (exit 1).

**Defect found by the smoke test and fixed:** swing/long-term freshness reported stored MOCK bars as `source: db, is_mock: false`. It now reports the stored bar's origin (`db:MOCK`, `is_mock: true`); test added.

## Design decisions

- Watchlist items reference `EXCHANGE:SYMBOL`, not an instrument row id, because each attribute change creates a new instrument version.
- A day's snapshot cannot be replaced by a different one for the same date (rejected); corrections go in the next day's snapshot. This keeps `valid_from < valid_to` strict and the history auditable.
- Mock instruments use synthetic `MOCK*` symbols with no ISIN, so they cannot be confused with listed companies.
- Exchange holidays are not bundled. Without a holiday list a holiday is treated as a trading day, which makes freshness report STALE (safe direction).
- Freshness of stored daily bars is evaluated from the database so backtests (Phase 3) and live checks use the same data path.

## Limitations carried forward

See KNOWN_LIMITATIONS.md (updated): holiday calendar source, Sharekhan instType→equity mapping, no real data source yet (B-06), no authentication (local only).

## Next: Phase 3 (awaiting approval)

Deterministic intraday baseline strategy (opening-range breakout + VWAP filter, versioned), event-driven backtest harness with a versioned cost model, paper order lifecycle and fill simulator, and the first dashboard screens — on MOCK data until a licensed data source is chosen.
