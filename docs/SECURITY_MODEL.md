# Security Model & Threat Model

**Status:** Phase 0 design; Phase 1 implemented redaction, audit chain, LIVE refusal and loopback-only binding.

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
