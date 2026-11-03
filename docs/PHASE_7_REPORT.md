# Phase 7 Report — Claude Research Service (Advisory)

**Date:** 2026-11-03 · **Status:** complete, awaiting owner review before Phase 8.
**Model calls made:** none. Every assessment in this phase came from the deterministic **MOCK** provider (`mock-research-1`). The Anthropic provider is implemented and tested against a simulated HTTP transport only. No API key exists in the repo, CI or this session. No order was created by research; LIVE remains locked.

## Scope and acceptance criteria

| Criterion (blueprint Phase 7) | Delivered |
|---|---|
| Evidence packets | `EvidencePacket` (schema `packet-1.0`) built only from point-in-time inputs. It holds facts (id, value, unit, source, source time), derived metrics (formula and input ids), assumptions, known gaps (`missing`) and untrusted documents. It is stored as canonical JSON with a SHA-256 **before** any model call (`research_packets`, append-only) |
| Versioned prompts | `assess-candidate@1.0.0`: system rules plus instructions plus the output schema. Its SHA-256 is stored in `prompt_versions`. Changing the text without a new version id is refused |
| Schema validation | `Assessment` (strict, unknown fields rejected; **no quantity, price, weight or order field exists**). The validator rejects:<br>• malformed or non-object JSON;<br>• oversize output (> 20 000 chars);<br>• actions outside CONFIRM / HOLD / NO_ACTION / REQUIRES_REVIEW;<br>• a wrong symbol, horizon or as_of;<br>• unknown source ids;<br>• key metrics that don't match their cited sources;<br>• numbers anywhere in the text that are not in the packet;<br>• CONFIRM without `evidence_sufficient` |
| Timeouts, retries, budget | Anthropic Messages API over httpx:<br>• timeout (30 s default);<br>• bounded retries with backoff on 408/409/429/5xx/529;<br>• no retry on 401/403.<br>The key is only ever sent as a header and never appears in an error or log.<br>Daily token budget (IST day, default 200 000, D-08): a call that could exceed it is not made. Per-cycle cap of 10 assessments. Cost is reported only when the owner sets current rates |
| Safe fallback | `assess()` never raises for provider or output problems. INVALID, TIMEOUT, ERROR, BUDGET_EXCEEDED and CYCLE_LIMIT are all stored, and the gate treats them as **REQUIRES_REVIEW** |
| LLM authority | `gate()` can only keep or downgrade a candidate:<br>• risk-increasing candidates (ENTER/ADD) proceed only on a valid CONFIRM;<br>• EXIT/REDUCE are never blocked by research;<br>• the runner's advisor hook runs only **after** the risk engine accepted, and uses only `allow` (quantity, price and side cannot change) |
| PAPER entry gate (optional) | `RHYTHM_RESEARCH__GATE_PAPER_ENTRIES=true` (default **off**). Held candidates are recorded with decision HOLD / NO_ACTION / REQUIRES_REVIEW, no order and no thesis. `strategy_signals.assessment_id` links the assessment |
| No path to orders | New import contracts:<br>• research cannot import broker, orders, execution, paper, risk, controls, portfolio, backtest or API;<br>• the decision core (risk, execution, orders, strategies, backtest, broker) cannot import research;<br>• packet, schema, prompts, validation, gate and providers are pure |
| Dashboard | New **AI research** page:<br>• service status (provider, MOCK tag, gate on/off, prompt hash, token budget meter);<br>• recent candidates with an **Assess** button and an optional untrusted document;<br>• assessments list;<br>• detail view: verdict, effect, bull/bear case, cited facts and metrics, uncertainties, rejected-output errors, raw reply for rejected output, full evidence packet with the untrusted document quoted and labelled |

**New endpoints:**
- `GET /v1/research/status`;
- `GET /v1/research/prompts`;
- `POST /v1/research/assessments` (`{signal_id, documents?}`; Idempotency-Key required, so a retry is not re-billed; 503 when not configured);
- `GET /v1/research/assessments?symbol=&signal_id=&status=`;
- `GET /v1/research/assessments/{id}` (packet, output, validation, raw excerpt, usage).

`/v1/signals` now includes `assessment_id`. A PAPER cycle response includes a `research` summary when the gate is on.

**Migration `0007_research`:**
- new tables `prompt_versions`, `research_packets` and `ai_assessments`, all append-only via triggers;
- new column `strategy_signals.assessment_id`.

Upgrade, downgrade and re-upgrade all ran clean, and `alembic check` reports no drift.

**Config:**
- `RHYTHM_RESEARCH_PROVIDER=none|mock|anthropic`, which requires `RHYTHM_FEATURES__RESEARCH_SERVICE=true`. `mock` is refused in staging/production.
- `anthropic` requires `RHYTHM_RESEARCH__MODEL` and a non-placeholder `RHYTHM_RESEARCH__API_KEY`.
- Other settings: `RHYTHM_RESEARCH__DAILY_TOKEN_BUDGET`, `__MAX_ASSESSMENTS_PER_CYCLE`, `__TIMEOUT_SECONDS`, `__MAX_RETRIES`, `__MAX_OUTPUT_TOKENS`, `__INPUT_USD_PER_MTOK` / `__OUTPUT_USD_PER_MTOK`, `__GATE_PAPER_ENTRIES`.
- `httpx` moved from a dev-only dependency to a runtime dependency.
- `demo.env` enables the MOCK provider (gate off). The demo script assesses the latest entry candidate with an injected "BUY 100000 shares" document attached.

## Commands run and results (exact)

Cloud session, Python 3.13 and 3.12.3, PostgreSQL 16, Node 22:

```
$ ruff check src tests scripts          → All checks passed!
$ ruff format --check src tests scripts → 155 files already formatted
$ mypy (strict)                         → Success: no issues found in 154 source files
$ lint-imports                          → Contracts: 14 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1)  → 398 passed, 1 warning; coverage 94 %
$ pytest on Python 3.12.3               → 398 passed, 1 warning
$ pip-audit --skip-editable             → No known vulnerabilities found
$ web: tsc --noEmit                     → ok
$ web: vitest run                       → Tests 8 passed (8)
$ web: next build                       → Compiled successfully (route /research built)
$ web: npm audit --omit=dev             → found 0 vulnerabilities
```

**New tests:**
- **34 unit tests** (`test_phase7_core.py`):
  - packet provenance and point-in-time correctness;
  - document sanitising;
  - prompt rules;
  - every validation rejection;
  - gate truth table;
  - MOCK determinism;
  - **prompt-injection fixture**;
  - Anthropic request shape, retries, timeout and auth errors without key leakage;
  - settings safety;
  - runner hook (holds without an order; CONFIRM leaves quantity unchanged; exits never consulted).
- **9 integration tests on PostgreSQL** (`test_phase7_api.py`):
  - not-configured behaviour;
  - end-to-end assessment with idempotent replay;
  - injection with a compromised MOCK model → INVALID / held;
  - budget exhausted → no call;
  - PAPER gate HOLD → no order or thesis;
  - PAPER gate CONFIRM → rules' order goes through;
  - provider timeout → fail closed;
  - append-only tables.

The warning is Starlette's TestClient deprecation notice.

**Demo run** (real API and PostgreSQL, MOCK data and MOCK provider, run with Python 3.9.25 as on the owner's Mac):
- long-term cycle → 1 PAPER order for MOCKE;
- research assessment of that candidate, with the injected document attached → `VALID CONFIRM`, gate `UNCHANGED`, "advisory only: no order was created or changed".

**UI smoke test** (Playwright + Chromium against that API): the AI research page showed the MOCK labels, "confirms candidate", the cited ROE / P/E / debt-equity with source ids, prompt `assess-candidate@1.0.0`, and the injected text quoted as "UNTRUSTED — data, not instructions".

**Defects found and fixed during the build:**
- the MOCK provider treated any false diagnostic (e.g. `new_results=false`) as a failed check → now only entry/quality checks count;
- a stored candidate's packet said "event calendar not synced" because the sync time was compared with the bar close instead of when the candidate was evaluated → fixed, with a regression assertion.

## What this does and does not show

It shows the plumbing and the safety properties:
- packets are frozen and traceable;
- malformed or over-reaching output is rejected;
- injected instructions cannot widen authority;
- failures fail closed;
- research cannot reach the order path except to hold a candidate.

It does **not** show the quality of a real model's judgement. No real model has been called. Before relying on assessments:
- choose a model and budget (D-08);
- supply the key from a secret store;
- run supervised sessions;
- review how often the strict number check rejects otherwise reasonable replies.

## Limitations carried forward

See KNOWN_LIMITATIONS.md:
- no real model run yet;
- no licensed news or filings source (documents are user-supplied only);
- strict number matching may reject some valid replies (fail closed);
- token counts from the MOCK provider are estimates;
- cost needs owner-supplied rates;
- the gate is global, not per strategy;
- no authentication until Phase 8.

## Next: Phase 8 (awaiting approval)

- full UX across the eight screens;
- approvals (human-in-the-loop for REQUIRES_REVIEW);
- notifications;
- audit views;
- authentication.
