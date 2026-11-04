# Phase 10 Report — Live-Readiness Review (Review Only)

**Date:** 2026-11-04 · **Status:** complete. **Verdict: NOT READY.** Live trading remains disabled and is not implemented. Nothing in this phase enables it, and no real order exists anywhere.

## Scope and acceptance criteria

| Criterion (blueprint Phase 10) | Delivered |
|---|---|
| Live-readiness **review only** | `docs/LIVE_READINESS_REVIEW.md`: 26 gates across system, security, operations, data, evidence, risk, regulatory, broker and owner. It includes a critical path, minimum safeguards if live is ever approved, and the questions to send to Sharekhan. |
| Checklist completed | `docs/COMPLIANCE_CHECKLIST.md` §1 rewritten from the **SEBI retail-algo circular itself** (SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013, 4 Feb 2025, applicable 1 Aug 2025; full PDF read on 2026-11-04), with paragraph references. Items that could not be verified are listed as such (§2). |
| Live stays off | `LIVE_TRADING_IMPLEMENTED = False` unchanged. `TRADING_MODE=LIVE` is still refused. The review module is read-only and contract-isolated, and tests confirm evaluation changes nothing. |
| Gate evaluation from the system | `rhythm.readiness.gates`, plus:<br>• `GET /v1/system/live-readiness` (any signed-in role);<br>• `rhythm-cli live-readiness [--markdown]`, which exits 1 while not ready;<br>• a **Live readiness** dashboard page. |

**Gates in the reference environment** (sign-in on, MOCK data): 4 met, 7 not met, 15 need you or an external party, 22 blocking.

## Research performed and its limits

- **Read:** the SEBI circular page and its 6-page PDF from sebi.gov.in; page 1 of SEBI's circular listing (no later algo amendment among the 25 most recent circulars); Sharekhan's Trading API page and API FAQ (no mention of static IP, algo ID or the SEBI framework; the FAQ rate limit is still the placeholder "XX").
- **Not obtainable in this session:**
  - web search is disabled for this organisation;
  - NSE circular and algo pages refused automated fetches;
  - the BSE notices page was a stale cached copy.

  So the **numeric orders-per-second threshold, the exchange implementation standards/FAQs, any later amendment and Sharekhan's static-IP/algo-ID procedure are unverified**. They are recorded as OWNER gates, not assumptions.

## Commands run and results (exact)

```
$ ruff check src tests scripts          → All checks passed!
$ ruff format --check src tests scripts → 190 files already formatted
$ mypy (strict)                         → Success: no issues found in 188 source files
$ lint-imports                          → Contracts: 17 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1)  → 445 passed, 3 deselected, 1 warning; coverage 92 %
$ pytest on Python 3.12.3               → 445 passed, 3 deselected, 1 warning
$ pip-audit --skip-editable             → No known vulnerabilities found
$ web: tsc --noEmit                     → ok
$ web: vitest run                       → Tests 10 passed (10)
$ web: next build                       → Compiled successfully (/readiness built)
$ web: npm audit --omit=dev             → found 0 vulnerabilities
$ rhythm-cli live-readiness --markdown  → exit 1; "NOT_READY · MET 4 · NOT_MET 7 · OWNER 15 · blocking 22"
```
**New tests:** 4 in `test_phase10_readiness.py`:
- NOT_READY on MOCK data, with the expected per-gate statuses;
- the scheduler gate flips when a heartbeat exists;
- evaluating changes nothing;
- real-data PAPER days counted correctly (MOCK days excluded);
- verdict logic and Markdown output;
- the CLI exits non-zero when not ready.

Perf and soak tests (Phase 9) are unchanged and excluded from the default run.

## Owner actions that unblock the most

1. Request the Sharekhan API key and Secret Key, and register the redirect URL.
2. Send the 10 questions in LIVE_READINESS_REVIEW §5 to skapi@sharekhan.com.
3. Choose a licensed data vendor (D-03).
4. Approve the read-only Sharekhan connection, the AWS deployment (fixed egress IP) and the start of a 60-trading-day real-data PAPER evaluation.

Live trading remains a separate decision after that evidence exists.
