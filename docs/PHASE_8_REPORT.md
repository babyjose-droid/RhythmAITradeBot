# Phase 8 Report — Sign-in, Approvals, Notifications, Audit Views, Eight Screens

**Date:** 2026-11-03 · **Status:** complete, awaiting owner review before Phase 9.
**Owner decisions applied:**
- **D-05:** password plus authenticator app (TOTP), cookie sessions, roles.
- **D-06:** human approval only for REQUIRES_REVIEW candidates.
- **Notifications:** in-app, plus an optional webhook.

**Data and accounts:** all data is MOCK and all orders are PAPER. No real credentials exist in the repo, CI or this session. The smoke-test user and secret key were throwaway values in a disposable database. LIVE remains locked.

## Scope and acceptance criteria

| Criterion (blueprint Phase 8) | Delivered |
|---|---|
| Authentication | **Passwords:** scrypt (stdlib, salted, N=2^15). Policy: at least 12 characters, must not contain the username, not repetitive.<br>**TOTP (RFC 6238):** 6 digits, 30 s step, ±1 step of drift allowed. Each code is accepted only once (replay protection). Secrets are encrypted at rest with Fernet, keyed from `RHYTHM_AUTH__SECRET_KEY`.<br>**Sessions:** cookie sessions (`HttpOnly`, `SameSite=Strict`, `Secure` outside local); the database stores only token hashes. Idle timeout 30 min; absolute limit 12 h.<br>**CSRF:** double-submit token in the `X-CSRF-Token` header on every mutating request.<br>**Brute-force protection:** lock after 5 failures for 15 min, plus a per-IP limit of 20 login attempts per 15 min. Error messages are generic, and a timing-equal dummy hash is checked for unknown users.<br>**Lifecycle:** logout revokes the session server-side; a password change revokes the user's other sessions. |
| Authorisation | Roles `owner`, `approver` and `viewer`, applied to **every** route by one dependency with a permission table:<br>• any role: GET requests, their own session and inbox;<br>• approver or owner: approval decisions and **activating** the kill switch;<br>• owner: everything else;<br>• owner **plus re-authentication within 5 minutes**: releasing the kill switch, resuming a strategy, new risk policies, ledger allocation, and user administration.<br>The signed-in user is the actor of every audit event.<br>Turning auth off is allowed in local/test only; staging/production refuse to start without auth, or with insecure cookies. Interactive API docs are not served outside local/test. |
| Users | `rhythm-cli user-create / user-reset-mfa / user-enable` (prompts for the password; the authenticator secret and `otpauth://` URI are printed **once**).<br>API for owners: list, create, disable or enable, and reset MFA. The last active owner cannot be disabled.<br>No default or seeded credentials exist. |
| Approvals | `approvals` table and queue:<br>• **What is queued:** REQUIRES_REVIEW entries/adds (risk REVIEW = order value above the policy's review threshold, or a research-gate hold), one open request per candidate.<br>• **Expiry:** intraday requests expire at the session close; daily ones at the next session's close.<br>• **Approve:** re-runs **every** risk check on current data through the same runner path as a cycle. An approval satisfies only the REVIEW policy (`OrderProposal.approved_by`, risk engine **1.3**); any FAIL still blocks. ACCEPT/REDUCE → PAPER order (+ thesis for daily horizons), status APPROVED. Otherwise the status is FAILED, with the failed checks recorded.<br>• **Reject / expire:** audited.<br>• **Recorded per decision:** who, when, IP, reason and the re-check result. |
| Notifications | In-app inbox: severity, unread counts, mark read. Triggered by:<br>• kill switch activated (critical) or released;<br>• strategy paused (daily loss stop = critical) or resumed;<br>• reconciliation break (critical);<br>• approval pending (warning).<br>**Optional webhook** (https only; Slack-compatible `text` plus structured fields):<br>• outbox written in the same transaction, so a rolled-back action never notifies;<br>• background dispatcher in the API process;<br>• minimum severity (default warning), exponential backoff, FAILED after 5 attempts;<br>• payloads carry no secrets, and the webhook URL is never logged or stored in errors. |
| Audit views | `GET /v1/audit/events` gains filters: event type (exact or `prefix*`), actor, subject, time range, paging. |
| 8 screens | Navigation: **Portfolio dashboard** (with a new "needs attention" panel), **Intraday**, **Swing**, **Long-term**, **AI research**, **Risk centre**, **Automation centre** (new: guards, strategies, last runs, manual PAPER run), **Audit & reconciliation** (new: chain integrity, filterable events with payload and hash, reconciliation history).<br>Also new: **Approvals**, **Notifications** (with a bell in the session bar), **Sign in**, **Account** (password change; owner user administration with a one-time secret display), and a **re-authentication dialog** that retries the action once after password + code.<br>The visual grammar still holds: a queued request shows **"IDEA — not an order"**, and a failed re-check is never shown as approved. |

**New endpoints:**
- auth: `/v1/auth/status | login | logout | me | reauth | password`;
- users: `/v1/users` and `/v1/users/{username}/disable | enable | reset-mfa`;
- approvals: `/v1/approvals`, `/v1/approvals/{id}`, `POST /v1/approvals/{id}/decision` (Idempotency-Key);
- notifications: `/v1/notifications`, `/summary`, `/read`;
- `/v1/automation`.

**Migration `0008_ops`:** `users`, `auth_sessions`, `approvals`, `notifications`. Upgrade, downgrade and re-upgrade all ran clean, and `alembic check` reports no drift. Migrations now read only `RHYTHM_DATABASE_URL`, so they never need the auth key.

**Config:**
- `RHYTHM_AUTH__ENABLED` (default **true**);
- `RHYTHM_AUTH__SECRET_KEY` (required when enabled, at least 32 characters, placeholders refused);
- session, lockout and re-auth timings;
- `RHYTHM_NOTIFICATIONS__WEBHOOK_URL` and `__WEBHOOK_MIN_SEVERITY`.

`cryptography` is a new runtime dependency (Fernet). `demo.env` turns sign-in **off** for the one-command local demo, and the dashboard then shows "AUTH OFF (local demo)".

## Commands run and results (exact)

Cloud session, Python 3.13 and 3.12.3, PostgreSQL 16, Node 22:

```
$ ruff check src tests scripts          → All checks passed!
$ ruff format --check src tests scripts → 171 files already formatted
$ mypy (strict)                         → Success: no issues found in 170 source files
$ lint-imports                          → Contracts: 15 kept, 0 broken.
$ pytest --cov (REQUIRE_INTEGRATION=1)  → 420 passed, 1 warning; coverage 93 %
$ pytest on Python 3.12.3               → 420 passed, 1 warning
$ pip-audit --skip-editable             → No known vulnerabilities found
$ web: tsc --noEmit                     → ok
$ web: vitest run                       → Tests 9 passed (9)
$ web: next build                       → Compiled successfully (/login /approvals /notifications /audit /automation /account built)
$ web: npm audit --omit=dev             → found 0 vulnerabilities
```

**New tests:**
- **10 unit tests** (`test_phase8_core.py`):
  - TOTP against the **RFC 6238 test vectors**, drift window, replay and format;
  - scrypt hashing and password policy;
  - secret box (wrong key fails);
  - login rate limiter;
  - risk engine: approval satisfies only REVIEW, and a kill switch still blocks;
  - approval expiry by horizon.
- **10 integration tests on PostgreSQL** (`test_phase8_api.py`):
  - login, session and CSRF; logout revocation; failed attempts audited;
  - code replay, lockout and idle timeout;
  - roles, plus re-authentication for kill-switch release and user admin;
  - password change revokes other sessions;
  - REVIEW candidate queued once → approved → PAPER order + thesis;
  - FAILED re-check under the kill switch, reject, expiry, notifications read;
  - approver decides, viewer cannot;
  - webhook outbox delivery, severity filter and backoff, with no URL in the payload;
  - automation overview and audit filters;
  - CLI user creation (secret shown once; duplicate and weak password refused).
- **Settings:** 2 new tests (auth required outside local/test; real secret key; https webhooks).
- **Existing tests:** run with auth explicitly off (`make_settings`).

The warning is Starlette's TestClient deprecation notice.

**Browser smoke test** (Playwright + Chromium against the real API with **auth on**, and PostgreSQL):
1. Opening `/approvals` signed out redirected to `/login?next=/approvals`.
2. A wrong code showed "invalid credentials"; the correct code signed in.
3. The queued MOCKE entry showed as "IDEA — not an order". Approving with a reason → "risk re-check passed; PAPER order … created (not yet filled)".
4. The dashboard, notifications, audit (chain **INTACT**; every action attributed to `owner`) and automation pages rendered with no console errors.
5. After sign-out, `/audit` redirected back to sign-in.

**Demo:** with `demo.env` (auth off), the one-command demo ran end to end on Python 3.9.25.

**Defects found and fixed during the build:**
- the approval re-check had no volume history, so R12 failed → it now loads the same bars a cycle uses;
- migrations would have needed the auth secret → they now read only the database URL;
- test passwords that contained the username were correctly refused by the policy.

## What this does and does not show

It shows:
- every API route requires a signed-in user with the right role;
- sensitive changes need a fresh password + code;
- CSRF and replay protections work;
- REQUIRES_REVIEW candidates reach a human, and approval can never bypass a failed risk check;
- alerts reach the inbox (and a webhook when configured);
- every action is attributable in the audit log.

It does **not** cover:
- hosted TLS: `Secure` cookies need HTTPS, which comes with the AWS deployment;
- a reviewed threat model for internet exposure.

## Limitations carried forward

See KNOWN_LIMITATIONS.md:
- the login rate limiter is in memory, per API process;
- behind the dashboard proxy, the API sees the proxy's IP (lockout per user still applies);
- no QR code (secrets are entered manually or via the `otpauth://` URI);
- no WebAuthn or recovery codes (an owner resets MFA with the CLI);
- no scheduler yet (Phase 9);
- the kill switch is global.

## Next: Phase 9 (awaiting approval)

Reliability:
- built-in scheduler with health checks;
- backups and restore drill;
- monitoring and alarms;
- load and soak tests;
- failure-injection tests;
- runbooks.
