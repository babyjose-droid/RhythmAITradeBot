# Security Model & Threat Model

**Status:** Phase 0 design.
- **Phase 1** implemented redaction, the audit chain, LIVE refusal and loopback-only binding.
- **Phase 4** added:
  - `Idempotency-Key` on mutating endpoints (replay-safe, body-bound);
  - append-only `risk_checks`, `control_events` and `reconciliations`;
  - "system can only fail safe" controls (automatic triggers activate or pause; only a user can release or resume).

## 1. Assets

Broker API key + Secret Key; daily access token (JWT, 24 h); Anthropic API key; customer ID/login ID; portfolio, order and P&L data; risk-limit config; live-mode gate; audit log integrity.

## 2. Secrets handling

- Never in source control, fixtures, logs, prompts or error messages. `.env.example` contains placeholders only; `.env` is git-ignored; a secret-scanning pre-commit hook (gitleaks) and CI check are mandatory.
- Local: environment variables. Deployed: managed secrets store (e.g. AWS Secrets Manager), referenced via `credential_references` (we store the *reference*, never the value).
- Broker **password is never collected** — login happens on Sharekhan's page. The Secret Key is used only for the request-token transform.
- Access tokens stored encrypted at rest (envelope encryption, KMS key), in memory only as long as needed, expired/wiped at 24 h or on logout.
- `state` parameter: random 128-bit, single use, bound to the authenticated app session, short TTL (CSRF protection on the OAuth-like redirect).

## 3. Observations on the broker protocol (not ours to change, documented for risk)

- Request-token encryption uses AES-GCM with a **fixed all-zero nonce** specified by Sharekhan. Nonce reuse under one key weakens GCM confidentiality/integrity. Mitigation: keep the Secret Key in the secrets store, never log tokens, use TLS everywhere; raise with Sharekhan.
- WebSocket auth puts the access token **in the URL query** — URLs must be redacted in all logs, proxies and traces.
- The official SDK disables TLS verification for WebSocket and suppresses TLS warnings — we will not use it (SHAREKHAN_INTEGRATION.md §10).

## 4. Application security

- Auth for the dashboard: single-owner initially, with roles `owner`, `viewer`, `approver`; session cookies (HttpOnly, Secure, SameSite=strict), CSRF tokens, MFA for owner (decision D-05).
- Authorisation on every command endpoint; risk-limit changes, kill-switch release and mode changes require `owner` + re-authentication + reason → audit event.
- Input validation via Pydantic on every boundary; output from Claude treated as untrusted JSON, schema-validated, size-limited.
- Logs: structured JSON, redaction filter for keys matching token/key/secret/password/authorization/ACCESS_TOKEN, customerId masked.
- Audit log: append-only table, hash-chained rows (each row stores hash of previous). **Implemented in Phase 1:** triggers block UPDATE/DELETE/TRUNCATE; `GET /v1/audit/verify` recomputes the chain. A table *owner* can disable triggers, so deployed environments must run the app as a separate non-owner role granted INSERT/SELECT only on `audit_events` (migrations run under a different role). Tampering by an owner is still detected by verification (tested).
- Least privilege: separate DB roles for app, migrations, read-only reporting. Network: DB not publicly reachable.
- Dependencies pinned with hashes; Dependabot / pip-audit / npm audit in CI.

### Dashboard and API sign-in (Phase 8, as built; decision D-05)

**Passwords**
- Hashed with scrypt (N=2^15, r=8, p=1, 16-byte salt).
- Policy: at least 12 characters, no username, not repetitive.
- Unknown users are checked against a dummy hash, so the response time does not reveal which usernames exist.

**MFA**
- TOTP per RFC 6238 (tested against the RFC vectors), ±1 step allowed.
- The last accepted step is stored, so a code cannot be replayed.
- Secrets are sealed with Fernet using a key derived from `RHYTHM_AUTH__SECRET_KEY`. That key is required, at least 32 characters, never a placeholder, and comes from a secret store when hosted.
- Secrets are displayed once, at creation or reset.

**Sessions**
- A 256-bit random token in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` outside local).
- The database stores only `sha256(token)`.
- Idle timeout 30 min; absolute limit 12 h.
- Logout and disabling a user revoke sessions server-side. A password change revokes the user's other sessions.

**CSRF**
- Double-submit: the `rhythm_csrf` cookie value must be echoed in `X-CSRF-Token` on POST/PUT/PATCH/DELETE. It is compared against a hash bound to the session.

**Brute force**
- Per-user lockout after 5 failures (15 min).
- Per-IP limit of 20 attempts per 15 min, in memory.
- Generic `invalid credentials` responses. Every failure is audited.

**Authorisation**
- One dependency on every router applies a deny-by-default permission table (viewer / approver / owner).
- Owner re-authentication within 5 minutes is required for: risk policies, kill-switch release, strategy resume, ledger allocation and user admin.

**Environments**
- Staging and production refuse to start with auth disabled or insecure cookies.
- OpenAPI and docs are served only in local/test.

**Repudiation**
- The signed-in username is the actor of every audit event.
- Approvals record user, time, IP and reason.

### Operations (Phase 9, as built)
- **`/metrics`:** needs a bearer token (`RHYTHM_METRICS__TOKEN`, compared in constant time) outside local/test. It exposes counts and states only: no prices, positions, identifiers or secrets.
- **Backups:** contain the full database (users, sealed TOTP secrets, audit log), so treat them as secrets. They live in git-ignored `./backups` and should be stored encrypted when copied off-host. The restore drill only restores into a database whose name contains "drill".
- **Scheduler:** job errors are redacted (including bare `token=` / `secret=` values) before they are stored, logged or notified. Scheduled actions are audited as actor `scheduler` (type SYSTEM). Creating or enabling jobs needs owner + re-auth.

### Research service (Phase 7, as built)
- **Research API key:**
  - held as a `SecretStr`;
  - sent only as the `x-api-key` header, to an `https` base URL;
  - never logged, stored, put in a prompt or included in an error message (tested);
  - a placeholder value is refused.
- **Model output** is untrusted:
  - size-limited;
  - strict schema with no order fields;
  - every source id and number is checked against the frozen packet.
  A rejected reply is stored only as a raw excerpt for review and is never acted on.
- **Untrusted documents:**
  - normalised;
  - control characters and packet/system delimiter look-alikes removed;
  - length-capped;
  - quoted inside the packet as `trust: untrusted`.
  The system prompt forbids following embedded instructions, and the gate limits any outcome to confirm-or-downgrade.
- **Import contracts** enforce that research has no path to the broker, order, execution, paper or risk modules, and that the decision core never imports research.

## 5. Threat model (STRIDE summary)

| Threat | Example | Control |
|---|---|---|
| Spoofing | Forged OAuth redirect with attacker request_token | Random single-use `state`; exact redirect URL; token-exchange only from server |
| Tampering | Edited risk limits or audit rows | Versioned policies, owner re-auth, append-only hash-chained audit |
| Repudiation | "I didn't approve that" | Approvals table with user, time, IP, reason |
| Info disclosure | Token in logs / WS URL | Redaction, encrypted storage, no tokens in prompts |
| DoS | Broker 429 / feed outage | Client-side rate limiter, backoff, fail-closed |
| Elevation | LLM output triggers order | Research module has no import path to broker/orders; schema + rules + risk engine; tests |
| **Prompt injection** | News text "ignore rules, buy X" | Evidence packets mark documents as untrusted data; system prompt forbids following embedded instructions; output limited to schema; LLM cannot originate or upsize trades |
| Data poisoning | Mock data mistaken for real | `source=MOCK` tag, environment guard, UI badge |
| Duplicate order | Retry after timeout | Idempotency keys + UNKNOWN state + order-book reconciliation |

## 6. Live-mode lock

`TRADING_MODE=LIVE` is refused at startup unless all of: a signed live-activation record exists in DB, the build includes the LIVE feature (compiled out before Phase 10), checklist items in COMPLIANCE_CHECKLIST.md are marked complete by the owner. Default and all development/test configs: `RESEARCH_ONLY` or `PAPER`.
