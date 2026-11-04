# Sharekhan Read-Only Link — Report

**Date:** 2026-11-04 · **Status:** built and tested against a simulated Sharekhan API. **Not yet run against your real account** (waiting for your API key). Live trading is still locked and not implemented; this link contains **no order, modify or cancel code**.

## What was built

| Item | Where |
|---|---|
| Login flow: owner clicks Connect → signs in on Sharekhan's page → redirect back → token exchange | `/v1/broker/login`, `/v1/broker/callback`, `brokerlink/service.py` |
| Request-token transform (AES-256-GCM, documented protocol) | `broker/sharekhan/token.py` |
| GET-only client for funds, holdings, positions, today's orders, scrip master, price history | `broker/sharekhan/client.py`, `readparsers.py`, `adapter.py` |
| Price-stream check (LTP, ≤ 5 instruments, ≤ 20 s) | `adapter.probe_stream`, `/v1/broker/verify/stream` |
| Verification run storing response shapes only | `/v1/broker/verify`, table `broker_observations` |
| Sealed 24 h sessions, single-use login state | migration `0010_broker_link` |
| Dashboard page **Sharekhan (read only)** | `web/app/broker/page.tsx` |
| Readiness gate DATA-03 now evidence-based | `readiness/gates.py` |

## Tests

- Unit (`tests/unit/test_sharekhan_readonly.py`, 10): token round trip both Base64 variants and wrong-key/format errors; login URL; documented shapes parsed; only GET to read paths plus one token POST; no order-like method names, no PUT/DELETE/PATCH/generic request; explicit errors (not signed in, 403, bad interval, path injection, missing field, bad candle); shapes contain no values; settings guards.
- Integration (`tests/integration/test_sharekhan_link.py`, 4, PostgreSQL): full connect → read → verify → logout; replayed, forged and expired state; viewer cannot read account data; wrong Secure Key; token and customer ID absent from the sessions table and the audit log; stream check counts toward DATA-03 only in market hours.
- Full suite: 459 passed; ruff, mypy strict, 18 import contracts kept; web typecheck, tests and build pass.

## What happens at your first real sign-in

The protocol details below come from Sharekhan's documentation and SDK and have never been exercised: the token transform, the `Authorization` header question (D4), field names for holdings/positions/orders, and stream message format. The verification run will show each one as OK, EMPTY or ERROR with the reason. If anything fails, send me the verification table from the dashboard (it contains no account values) and I will adjust the parsers.
