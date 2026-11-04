# Phase 9 Report — Reliability: Scheduler, Monitoring & Alarms, Backups, Load/Soak/Failure Tests, Runbooks

**Date:** 2026-11-03 · **Status:** complete, awaiting owner review before Phase 10.
**Data and accounts:** all data is MOCK and all orders are PAPER. No broker connection exists yet (Sharekhan read-only integration is pending your API key). No credentials were used. LIVE remains locked.

## Scope and acceptance criteria

| Criterion (blueprint Phase 9) | Delivered |
|---|---|
| Scheduling, recovery | **Process and storage:** a `rhythm-scheduler` process (new compose service). Jobs live in `scheduled_jobs`; runs in `job_runs`.<br>**Kinds:** `paper_cycle`, `events_sync`, `fundamentals_sync`, `health_check`, `audit_verify`.<br>**Schedules** (exchange calendar, IST):<br>• `intraday_5m`: 09:20–15:30, every 5 min, 75 slots per session;<br>• `after_close`: 15:45;<br>• `daily_at` (HH:MM);<br>• `every_minutes` (N).<br>**One leader:** a PostgreSQL advisory lock; a second instance stands by and takes over when the leader goes away.<br>**One run per slot:** the run row is inserted and committed before the work starts, with a unique (job, slot) constraint.<br>**Isolation:** each job runs in its own transaction, so a failure leaves no partial writes.<br>**No catch-up storms:** slots missed by more than 10 minutes are recorded once as MISSED and not run late.<br>**Failures:** redacted errors; a warning on each failure; **auto-disable after 3 consecutive failures**, with a critical alarm.<br>**Defaults:** jobs are created disabled; creating or enabling one needs owner + re-auth.<br>The intraday cycle checks that a strategy only runs on its own schedule. |
| Monitoring & alarms | **Readiness:** includes a scheduler heartbeat check (stale after 120 s → not ready) when a scheduler is expected (compose default). `rhythm-cli scheduler-health` serves as the container healthcheck.<br>**Health-check job** (components: kill switch, reconciliation, scheduler, jobs, webhook backlog): records each transition in `system_health_events` and alarms on change only (down → critical, degraded → warning, recovered → info).<br>**`/metrics`:** Prometheus text format, bearer-token protected; without a token it is served in local/test only. It reports HTTP counters and a latency histogram by route template, plus gauges for kill switch, paused strategies, pending approvals, unread notifications, webhook backlog, scheduler heartbeat age, per-job failures, per-account reconciliation and audit row count. |
| Backups & restore | **Backups:** `scripts/backup.sh` (compose `backup` service, daily). It writes a `pg_dump` custom-format file plus SHA-256, read back with `pg_restore --list` before it gets its final name; 14-day retention; written to git-ignored `./backups`.<br>**Restore drill:** `scripts/restore_drill.sh` checks the checksum, restores into a scratch `*_drill` database only, runs `rhythm-cli restore-verify` (schema at head, audit hash chain intact, counts) and drops the scratch database. A Docker-friendly variant uses `--database-name`. |
| Rate limits / resilience | **Inherited and re-tested:**<br>• login rate limiting and lockout (Phase 8);<br>• research budget and retries (Phase 7);<br>• webhook backoff (Phase 8);<br>• market-data outage: entries are skipped, never guessed (R4);<br>• fail-closed risk checks.<br>**Redaction:** now also masks bare `token=` / `secret=` values in error text. |
| Load / soak / failure injection | **Load test:** `scripts/loadtest.py` (concurrent, read-only).<br>**Perf tests:** `pytest -m perf`.<br>**Soak test:** `pytest -m soak` (5 simulated trading days driven only by the scheduler).<br>**Failure-injection tests:** listed below. |
| Runbook rehearsed | OPERATIONS_RUNBOOK rewritten: scheduler, monitoring and alert rules, backups, the restore drill, a real-restore procedure, and an incident table (not ready, stale heartbeat, auto-disabled job, reconciliation break, broken audit chain, webhook failures, stale data, secret leak, broker token expiry).<br>Rehearsed in this session: backup → verify → restore drill; tampered dump detected; a real scheduler process observed for 75 s; readiness flips not-ready / ready with the heartbeat. |
| Dashboard | **Automation centre:**<br>• scheduler status (RUNNING / NOT RUNNING plus heartbeat age);<br>• component health;<br>• scheduled jobs table (enable/disable, run now, last and next run, auto-disable reason) with recent runs;<br>• add-a-job form;<br>• guards summary showing enabled job count. |

**New endpoints:**
- `GET /v1/scheduler/status`;
- `GET|POST /v1/scheduler/jobs`;
- `POST /v1/scheduler/jobs/{id}/enable | disable | run`;
- `GET /v1/scheduler/jobs/{id}/runs`;
- `GET /metrics`.

**Migration `0009_scheduler`:** `scheduled_jobs`, `job_runs` (unique per slot), `scheduler_heartbeats`. Upgrade, downgrade and re-upgrade all ran clean, and `alembic check` reports no drift.

**Compose:** new services `scheduler` (healthcheck: `rhythm-cli scheduler-health`) and `backup` (`postgres:16-alpine`). The API environment gains `RHYTHM_SCHEDULER__EXPECTED` (default true) and `RHYTHM_METRICS__TOKEN`.

**CLI:**
- `rhythm-scheduler`;
- `rhythm-cli restore-verify --database-url | --database-name`;
- `rhythm-cli scheduler-health`.

## Commands run and results (exact)

Cloud session, Python 3.13 and 3.12.3, PostgreSQL 16, Node 22:

```
$ ruff check src tests scripts          → All checks passed!
$ ruff format --check src tests scripts → 187 files already formatted
$ mypy (strict)                         → Success: no issues found in 185 source files
$ lint-imports                          → Contracts: 16 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1)  → 441 passed, 3 deselected, 1 warning; coverage 92 %
$ pytest on Python 3.12.3               → 441 passed, 3 deselected, 1 warning
$ pytest -m perf                        → 2 passed
$ pytest -m soak  (SOAK_DAYS=5)         → 1 passed
$ pip-audit --skip-editable             → No known vulnerabilities found
$ web: tsc --noEmit                     → ok
$ web: vitest run                       → Tests 10 passed (10)
$ web: next build                       → Compiled successfully
$ web: npm audit --omit=dev             → found 0 vulnerabilities
```
The 3 deselected tests are the perf and soak tests: they are excluded from the default run and CI, and were run separately (above). The warning is Starlette's TestClient deprecation notice.

**Performance** (budgets from PRODUCT_REQUIREMENTS §8):

| Measure | Result | Budget |
|---|---|---|
| Risk engine, 2,000 evaluations | p50 0.053 ms, p95 0.087 ms | < 50 ms |
| Dashboard API, 15 endpoints × 40 calls, in-process, seeded DB | worst p95 12.3 ms (`/v1/watchlists`) | < 300 ms |
| **Load test** against a live uvicorn API and PostgreSQL, 8 concurrent workers, 20 s | **3,140 requests (157 req/s), 0 errors; p50 45.9 ms, p95 91.9 ms, p99 124.6 ms**; slowest endpoint p95 134.6 ms (`/health/ready`) | p95 < 300 ms |

**Soak test** (5 simulated trading days, continuous 5-minute ticks, scheduler only):
- 1,540 ticks; tick p50 15 ms, p95 301 ms, max 1,177 ms (ticks that ran cycles);
- every slot ran exactly once and succeeded: 375 intraday cycles, 6 swing and 6 long-term cycles, 5 event syncs, health checks every 15 minutes, 6 audit verifications;
- **no FAILED or MISSED runs**;
- intraday books flat after every close;
- 69 PAPER orders; 774 reconciliations, all OK;
- audit chain intact;
- traced memory growth 1.2 MB.

**Failure injection** (12 integration tests in `test_phase9_api.py`, plus 9 unit tests in `test_phase9_core.py` for slots and metrics):
- an injected job failure rolled back its partial audit write, recorded FAILED with the secret redacted, and auto-disabled the job after 3 failures with two warnings and one critical alarm. The health check then reports `jobs: down`;
- two schedulers: only the lock holder runs; a duplicate slot claim is refused; the standby takes over after the leader releases;
- the database rejects a second run row for the same slot (`uq_job_runs_slot`);
- the scheduler was "down" for 4 days → one MISSED record, nothing run late, no orders;
- market data outage during a scheduled cycle → SUCCESS with every instrument skipped as "unavailable" and **no orders**;
- database unreachable → the tick raises (caught by the loop) and the process stays alive; it recovers as soon as the database is back;
- readiness: no heartbeat → not ready; heartbeat → ok; stale heartbeat → not ready;
- health alarms fire on state changes only (kill switch degraded → recovered), never repeated while steady;
- event sync, fundamentals sync and audit verification jobs; run-now via the API; `/metrics` (401 without the token, content checked with it).

**Backup / restore rehearsal** (real `pg_dump` / `pg_restore` 16 in the cloud session):
1. Backup → checksum OK → drill restored into `rhythm_restore_drill` → `restore-verify` returned `ok: true` (schema `0009_scheduler` = head; audit chain 19 rows intact; 1 account, 1 order) → scratch database dropped.
2. The first drill attempt was **correctly refused**: the source database was still on migration 0008.
3. A deliberately corrupted dump failed the SHA-256 check before any restore.
4. `--database-name` refuses names without "drill".

**Live scheduler process:** `rhythm-scheduler` ran for 75 s against the API and database with three enabled jobs. It became leader, ran the 1-minute health check on each slot and a manual audit check; readiness flipped from `not_ready` (no heartbeat) to `ready`; `scheduler-health` exited 0. The Automation centre showed RUNNING, component health and the job runs, with no browser console errors.

**Defects found and fixed during the build:**
- a released scheduler kept its advisory lock because pooled connections stay open → it now unlocks and invalidates the connection;
- error redaction missed bare `token=` / `secret=` values → added;
- the soak test first flagged MISSED slots because the simulation skipped nights (correct scheduler behaviour) → the soak is now continuous;
- the `rhythm-scheduler` console script needs a package reinstall to appear (this applies to the Docker image too, which installs fresh).

## What this does and does not show

It shows:
- the system runs unattended on a schedule, with one leader;
- slots are never doubled;
- failures roll back and escalate;
- missed time is reported, not replayed;
- alarms fire on real state changes;
- backups are verified restorable;
- latency is well within budget at this scale.

It does **not** cover:
- hosted monitoring (Prometheus/Grafana or CloudWatch scraping `/metrics`), which comes with the AWS deployment;
- multi-week soak on real market data;
- broker-side failure modes: no Sharekhan connection exists yet.

## Limitations carried forward

See KNOWN_LIMITATIONS.md:
- the scheduler runs only jobs that an owner creates;
- no web UI for the restore drill;
- backups are on the same machine, so copy `./backups` off-host;
- metrics are pull-only, with no built-in alert manager;
- the soak test uses MOCK data;
- the Sharekhan read-only integration is pending your API key and redirect-URL registration.

## Next: Phase 10 (awaiting approval)

A live-readiness **review only**:
- compliance checklist;
- Sharekhan written confirmations;
- static IP and algo requirements;
- an extended PAPER evaluation on real data.

LIVE stays off until a separate, explicit approval.
