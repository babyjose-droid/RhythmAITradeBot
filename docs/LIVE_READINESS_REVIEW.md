# Live-Readiness Review (Phase 10)

**Date:** 2026-11-04 · **Verdict: NOT READY.** Live trading stays locked (`LIVE_TRADING_IMPLEMENTED = False`; `TRADING_MODE=LIVE` is refused at start-up). The project contains no code that can place a real order.
**Nature of this document:** an engineering readiness review, **not legal, tax or investment advice**. It changes no setting. Enabling live trading would need:
- every blocking gate below met;
- a separate, explicit written decision by the account owner;
- a new, reviewed change that implements and tests the live order path.

The same gates are evaluated from the running system by `GET /v1/system/live-readiness`, `rhythm-cli live-readiness [--markdown]` and the **Live readiness** dashboard page.

## 1. Result at a glance

26 gates: **4 met, 7 not met, 15 need you or an external party.** This is the evaluation of the reference PAPER environment with sign-in on and MOCK data.

| Area | Met | Open (blocking) |
|---|---|---|
| System | live path locked (SYS-01, informational); audit chain intact (SYS-03) | live broker order adapter not built (SYS-02) |
| Security | sign-in with password + authenticator; owner exists (SEC-01) | hosted TLS + internet threat review (SEC-02) |
| Operations | reconciliation clean (OPS-03) | scheduler must run in the target environment (OPS-01); restore drill within 30 days + off-host backups (OPS-02) |
| Data | — | real market data (DATA-01); licensed fundamentals/events (DATA-02); Sharekhan read-only verification (DATA-03) |
| Evidence | — | ≥ 60 trading days of PAPER on real data (EVAL-01); cost model verified against a contract note (EVAL-02) |
| Risk | — | owner-approved real-money limits (RISK-01) |
| Regulatory | — | OAuth + 2FA (REG-01); **static IP** (REG-02); **exchange algo ID tagging** (REG-03); orders-per-second threshold / registration (REG-04); own/family use, white box (REG-05); exchange standards reviewed (REG-06) |
| Broker | — | product types and square-off (BRK-01); rate limits (BRK-02); discrepancies D1–D8 and funds formula (BRK-03); terms of use (BRK-04) |
| Owner | — | legal/tax advice (OWN-01); written decision to build and enable live (OWN-02) |

## 2. What the regulation requires (verified) and what is still unknown

Read from SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 (4 Feb 2025, applicable 1 Aug 2025). Details and paragraph numbers are in COMPLIANCE_CHECKLIST.md §1.

- **Applies to this project:**
  - **no open APIs**: access only with a client-specific API key from a **static IP whitelisted by the broker**;
  - **OAuth + two-factor** authentication;
  - every algo order **tagged with an exchange-provided unique identifier**;
  - self-developed algos need registration through the broker **only above an orders-per-second threshold**, and are usable by self and family only;
  - black-box algos need Research-Analyst registration.

  Our rules-based, documented strategies are white box, run at a few orders per day, and serve the owner's own account only.
- **Unknown** (could not be retrieved; web search is disabled for this organisation and NSE refused automated fetches):
  - the numeric threshold;
  - the NSE/BSE implementation standards and FAQs;
  - whether any later circular amended the framework;
  - how Sharekhan attaches the algo ID (its documented order payload has no such field);
  - Sharekhan's static-IP procedure.

  These remain **OWNER** gates, never assumptions.

## 3. Critical path (suggested order)

1. **Sharekhan access (you):**
   - obtain the API key and Secret Key;
   - register the redirect URL (B-04);
   - send the questions in §5 to skapi@sharekhan.com.
2. **Read-only connection (engineering, after your approval):** build the read-only Sharekhan adapter, then run one supervised session (login, funds, holdings, positions, history, streaming during market hours). No order endpoint is called. → DATA-03.
3. **Real data for PAPER:** Sharekhan market data for prices, plus a licensed vendor (D-03) for longer history, fundamentals and events. → DATA-01/02.
4. **Hosting with a fixed egress IP:** the AWS deployment (TLS, private RDS, Secrets Manager, NAT gateway + Elastic IP). Register that IP with Sharekhan. → SEC-02, REG-02.
5. **Extended PAPER evaluation:** at least 60 trading days on real data with the scheduler, reconciliation and alarms running. Then a written review of results, costs (verified contract-note rates), incidents and drawdowns. → EVAL-01/02, OPS-01/02.
6. **Owner decisions:** real-money risk limits (RISK-01), legal/tax advice (OWN-01), and only then a written decision (OWN-02) to start a **separate** project that implements and tests the live order path (SYS-02). That path would need exchange algo-ID tagging (REG-03), broker static IP, idempotent order submission with UNKNOWN reconciliation (designed in Phase 4) and a staged rollout (below).

Earliest possible date for EVAL-01, if real-data PAPER started today: about 12 weeks out. The dashboard shows the computed date.

## 4. If live is ever approved: minimum safeguards (design, not built)

- **First-stage limits:** a small, separately funded allocation; per-order and per-day notional caps far below the PAPER limits; delivery only to start; intraday later, after square-off rules (B-02) are confirmed.
- **Human approval:** every live order goes through the approvals queue (APPROVAL_PENDING → APPROVED); no unattended live orders in the first stage.
- **Broker reconciliation:** broker positions, orders and funds reconciled before and after every cycle. Any difference triggers the kill switch (already built for PAPER).
- **Dual kill switch:** ours, plus the broker's and exchange's per-algo controls. A documented manual flatten procedure using Sharekhan's own app.
- **Rollback:** a single setting returns the system to PAPER/RESEARCH_ONLY. The daily token expiry means nothing trades without the daily human login.
- **Threat model additions:** compromise of the static-IP host, leaked broker key or session token, replay of order requests, LLM output reaching the order path (already blocked by import contracts and the advisory gate).

## 5. Questions to send to Sharekhan (skapi@sharekhan.com)

1. Under SEBI's retail-algo framework (circular of 4 Feb 2025), how does a retail client using your Trading API register a **static IP**? How many IPs are allowed, how do changes work, and what is the lead time?
2. How is the **exchange-provided algo identifier** attached to orders placed through `POST /skapi/services/orders`? Is a registration step needed for a self-developed white-box strategy used only for my own account?
3. What is the **orders-per-second threshold** above which you require algo registration, and do you apply any lower limit to API clients?
4. Does your login flow (browser login → request token → access token) meet the **OAuth + 2FA** requirement? Will a daily login remain mandatory?
5. Exact **product types** for intraday versus delivery cash-equity orders, and the auto square-off time (B-02).
6. Real **rate limits** for order and data endpoints (the FAQ shows "XX") (B-03).
7. How do you register the **redirect URL** for the login flow (B-04)?
8. The formula for **available funds/limits** for API clients (B-05), and confirmation of the endpoint discrepancies D1–D8 listed in our integration notes (B-08).
9. Do you offer a **sandbox/UAT** environment (B-09)? Is there a **client order ID** or idempotency field (B-10)?
10. Terms of use for storing your market data for personal research and backtesting.

## 6. What was checked automatically

`rhythm.readiness.gates.evaluate` reads live state:
- the LIVE code flag;
- audit-chain verification;
- active owner users and auth settings;
- the scheduler leader heartbeat;
- the last reconciliation per PAPER account;
- data-source settings (MOCK versus real);
- the count of trading days with **non-MOCK** PAPER cycles in the audit log;
- the cost-model `verified` flag;
- risk policy versions.

It is read-only. An import contract forbids it from reaching the broker, order path, paper service, research or API modules, and tests confirm that evaluating it changes nothing.
