# Operations Runbook

**Status:** Phase 9: scheduler, monitoring, backups/restore drill and incident playbooks written and rehearsed in the session (see PHASE_9_REPORT.md). Phase 4–8 procedures below.

## Daily (trading days, IST)

1. Before 09:00 — owner completes Sharekhan login (token valid 24 h, no refresh documented); system verifies session, clock sync, data feeds, reconciliation; mode shown on dashboard.
2. 09:15–15:30 — monitor freshness, risk utilisation, alerts.
3. After close — the scheduler runs the swing/long-term cycles at 15:45 IST. Then check the Approvals queue, critical notifications, job failures, and that last night's backup exists in `./backups`.

## Scheduler (Phase 9)

- **Process:** `rhythm-scheduler` (compose service `scheduler`). Run one per deployment; extra replicas stand by. Only the holder of the PostgreSQL advisory lock runs jobs, and `job_runs` is unique per (job, slot), so a slot can never run twice.
- **Jobs:** create and enable them under Automation → Scheduled jobs (owner + re-auth). They are created disabled.
  - Intraday PAPER cycles run every 5 minutes, 09:20–15:30 IST.
  - Swing and long-term cycles run at 15:45 IST on trading days.
  - Suggested extras:
    - event-calendar sync daily at 08:30;
    - fundamentals sync after the close;
    - a health check every 5 minutes;
    - an audit-chain check daily at 18:00.
- **Missed slots:** if the scheduler was down for longer than `missed_grace_minutes` (10), the missed slots are recorded once as MISSED and **not** run late. Cycles then resume from the next slot.
- **Failures:** each run is its own transaction, so a failure leaves no partial writes. Each failure raises a warning notification. After 3 in a row the job is **auto-disabled** with a critical notification. Fix the cause (see the run's error) and re-enable it.

## Monitoring and alarms (Phase 9)

- **Readiness:** `/health/ready` includes a `scheduler` check when `RHYTHM_SCHEDULER__EXPECTED=true` (the compose default). A leader heartbeat older than 120 s makes the API **not ready**.
- **CLI check:** `rhythm-cli scheduler-health` exits 0 or 1 (it is the container healthcheck).
- **Health-check job** (components: kill switch, reconciliation, scheduler, jobs, webhook backlog):
  - stores every state change in `system_health_events`;
  - notifies down → critical, degraded → warning, recovered → info;
  - repeats nothing while a state is steady.
- **Metrics:** `GET /metrics` in Prometheus text format, with `Authorization: Bearer $RHYTHM_METRICS__TOKEN`; without a token it is served in local/test only. Suggested alerts:
  - `rhythm_scheduler_heartbeat_age_seconds > 120`;
  - `rhythm_kill_switch_active == 1`;
  - `rhythm_reconciliation_ok == 0`;
  - `rhythm_job_consecutive_failures > 0`;
  - `rhythm_webhook_backlog{status="FAILED"} > 0`;
  - `rhythm_approvals_pending > 0` near the close;
  - HTTP 5xx rate and p95 latency from `rhythm_http_request_duration_seconds`.

## Backups and restore (Phase 9)

**Backups**
- The compose `backup` service runs `scripts/backup.sh` daily.
- Each run writes a `pg_dump` custom-format archive plus a `.sha256` file into `./backups` (git-ignored; it contains account data, so keep it private).
- Dumps older than 14 days are deleted.
- A dump is only renamed into place after `pg_restore --list` has read it back.

**Restore drill** (monthly, and after any schema change). With Docker on the Mac:
```bash
cd ~/Projects/rhythm-ai-trader
docker compose run --rm -e VERIFY_CMD=none -e KEEP_DRILL_DB=1 backup sh /scripts/restore_drill.sh
docker compose exec api rhythm-cli restore-verify --database-name rhythm_restore_drill
docker compose exec db dropdb -U rhythm rhythm_restore_drill
```
On a machine with PostgreSQL tools and the Python package, `scripts/restore_drill.sh` does all three steps in one go (it was run like that for the Phase 9 report).

The drill:
1. checks the SHA-256;
2. restores into a scratch `rhythm_restore_drill` database (it refuses any other name);
3. runs `rhythm-cli restore-verify` (schema at head, audit hash chain intact, row counts);
4. drops the scratch database.

**Real restore (data loss)**
1. Stop `api`, `scheduler` and `web`.
2. `pg_restore --clean --if-exists --no-owner -d rhythm <dump>`.
3. `docker compose run --rm migrate`.
4. Start the services.
5. Run a reconciliation for every PAPER account and `GET /v1/audit/verify`.
6. Check the Approvals queue: requests that expired during the outage stay expired.

## Incidents

| Symptom | First checks | Action |
|---|---|---|
| Dashboard says "not ready" | `/health/ready` checks | **database:** `docker compose ps db`, disk space. **migrations:** `docker compose run --rm migrate`. **scheduler:** `docker compose logs scheduler`, restart it. **clock_skew:** fix host NTP. |
| Scheduler heartbeat stale | `docker compose logs scheduler`; `rhythm-cli scheduler-health` | Restart the service. Slots missed by more than 10 minutes show as MISSED and are not run late, which is intended. |
| Job auto-disabled | Automation → job → recent runs (error) | Fix the cause (missing provider, deleted watchlist, paper mode off), then re-enable (owner + re-auth). |
| Reconciliation break (critical alarm; kill switch is on) | Audit & reconciliation → differences | Find the cause before releasing the kill switch. Release needs owner + re-auth. |
| Audit chain broken (critical; kill switch is on) | `GET /v1/audit/verify` → first bad row | Treat as tampering or a storage fault. Restore from the last backup that verifies, then investigate. |
| Webhook FAILED | Notifications → webhook column | Check the endpoint and URL secret. In-app notifications are unaffected. |
| Data stale / market data down | Cycle `skipped` reasons; freshness | Entries are blocked automatically (R4); exits still work. Wait, or switch the source. |
| Suspected secret leak | — | Rotate `RHYTHM_AUTH__SECRET_KEY` only together with an MFA reset for all users. Rotate the API keys at the provider, the webhook URL and the metrics token. Revoke sessions by disabling and re-enabling users. |
| Broker 403 / token expired (after the Sharekhan connection exists) | — | Daily login required. The system degrades to research-only, fail closed. |

## Health

`/health/live`, `/health/ready` (including the scheduler heartbeat), `/metrics`, `system_health_events`, and the notifications inbox.

## Swing routine (PAPER, Phase 5)

After the close (from about 15:45 IST): sync the event calendar (at least weekly; R18 requires ≤ 7 days), run the PAPER swing cycle, then review the Swing workspace:
- theses marked INVALIDATED have an exit queued for the next open;
- watch the "never — entries blocked" coverage rows.

The next morning's cycle is not needed: fills happen when the next after-close cycle processes that session.

## Long-term routine (PAPER, Phase 6)

After the close: sync fundamentals (daily is fine — nothing is rewritten) and the event calendar (weekly), run the long-term cycle, then review the Long-term page: new-results reviews, any REDUCE/ADD rebalances, and INVALIDATED theses (exit queued for the next open). Remember every fundamental number is MOCK.

## Users, sign-in and approvals (Phase 8)

**First run**
1. Set `RHYTHM_AUTH__SECRET_KEY` (keep a secure copy: losing or changing it invalidates every enrolled authenticator).
2. Run `docker compose exec api rhythm-cli user-create --username owner --role owner`.
3. Add the printed secret to an authenticator app.

**Common fixes**
- **Locked out:** wait 15 minutes, or run `rhythm-cli user-enable --username <name>`.
- **Lost authenticator:** run `rhythm-cli user-reset-mfa --username <name>`, or an owner uses Account → Reset authenticator. This revokes that user's sessions.
- **Suspected session theft:** change the password (other sessions are revoked) or disable the user.

**Daily checks**
- Clear the **Approvals** queue before it expires: intraday at the close, daily horizons by the next close.
- A FAILED approval means the risk re-check refused it; read the reasons and do not retry blindly.
- Review **Notifications**: critical items (kill switch, reconciliation break, daily loss stop) need action the same day.
- If a webhook shows FAILED, check the endpoint. Delivery is retried up to 5 times.

## Research service (Phase 7, advisory)

- **Status and budget:** `GET /v1/research/status`, or the AI research page. The budget resets at IST midnight. When it is exhausted, assessments are stored as BUDGET_EXCEEDED without a call.
- **If the PAPER entry gate is on:** any non-CONFIRM result holds the entry. Typical causes are timeouts, invalid replies or an exhausted budget. Held candidates appear with decision HOLD / NO_ACTION / REQUIRES_REVIEW, and their assessment is linked from the candidate row. To proceed without research, turn the gate off and restart. Exits are never affected.
- **Invalid replies:** read the errors and the raw excerpt on the assessment page. Never copy numbers from a rejected reply.
- **Key handling:** if a key is ever exposed, revoke it at the provider and replace it in the secret store. Never put it in `.env` files that are committed, or in chat.

## Phase 4 control procedures (PAPER)

- **Kill switch:**
  - Activate: dashboard → Risk & controls → type a reason → *Activate kill switch*, or `POST /v1/risk/kill-switch {"action":"activate","reason":...}` with an `Idempotency-Key`.
  - Effect: open entry orders are cancelled, and stops/targets remain. Tick *flatten* to also square off intraday positions on the next cycle.
  - Release: same screen, with a new reason. Only a person can release.
- **Reconciliation break:**
  - The kill switch activates automatically (actor `system:reconciliation`).
  - Open Portfolio & reconciliation and read the diffs (cash / position / order-fill / orphan fill).
  - Find the cause before releasing. The fill journal is append-only and is the reference.
- **Daily loss stop:** the strategy is auto-paused (`system:risk-engine`). Review the day's decisions in Risk & controls → Recent risk decisions, then resume with a reason, or leave it paused.
- **UNKNOWN order** ("UNKNOWN — check"):
  - Never re-send. The next cycle matches it against the venue order book.
  - If it stays ambiguous, compare the venue's day orders by hand.
  - New orders on that instrument stay blocked (R16) until the order is resolved.
- **Risk policy change:** `POST /v1/risk/policies {"params": {...}, "reason": ...}` creates a new version, effective immediately. Values outside the hard bounds are rejected.
