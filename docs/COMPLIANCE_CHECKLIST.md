# Compliance Checklist

**Status:** Phase 10 review (2026-11-04); see LIVE_READINESS_REVIEW.md for the gate-by-gate assessment. This is an engineering checklist, **not legal advice**. Every item must be confirmed against current primary sources and with Sharekhan before any live activation. Live trading stays disabled until every box is ticked by the account owner.

## 1. SEBI retail-algo framework — verified from the circular (2026-11-04)

**Source:** SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013, *Safer participation of retail investors in Algorithmic trading*, 4 Feb 2025. The full PDF (6 pages) was read from sebi.gov.in on 2026-11-04. Paragraph numbers refer to that PDF. This is an engineering summary, **not legal advice**.

| Topic | What the circular says | What it means for this project |
|---|---|---|
| Effective date | Implementation standards by the Broker's Industry Standards Forum by 1 Apr 2025 (para 7(a)). Applicable from **1 Aug 2025** (para 7(b)). | In force now, unless later changed. No extension was found on page 1 of SEBI's circular listing; other pages could not be checked. |
| Broker as principal | Brokers are principals; algo providers and vendors act as their agents through the broker's API (para 5, I(a)). | All live access goes through Sharekhan's API under Sharekhan's rules. |
| No open APIs; static IP | Access only "through a unique vendor client specific API key and static IP whitelisted by the broker" (para I(d)). | Needs a **fixed egress IP** (for example AWS NAT gateway + Elastic IP) registered with Sharekhan. A home or office dynamic IP will not do. |
| Authentication | OAuth-based authentication only, plus two-factor authentication for API access (para I(d)). | Sharekhan's browser login plus request token looks OAuth-like; confirm compliance in writing. |
| Order tagging | Algo orders are tagged with a **unique identifier provided by the stock exchange** (para I(b), II(b)). | Sharekhan's documented order API has **no algo-ID field**: written confirmation needed on how tagging works. |
| Self-developed algos | Registration through the broker is needed **only above the orders-per-second threshold**. Once registered, the algo may be used for self, spouse, dependent children and dependent parents, not others (para I(c)). | Expected use is far below any plausible threshold (a few orders per day). The **number** is set by the Broker's Industry Standards Forum and was **not retrieved**: confirm it. |
| Threshold detection | Brokers must detect and categorise orders above the threshold as algo orders (para I(d)). | The broker may classify us; keep order rates low and documented. |
| White box vs black box | Black-box algos (logic not disclosed) need the provider to register as a Research Analyst (para V(a)). | Our strategies are rules-based, documented and replicable (white box). Offering them to others would change the position. |
| Kill switch / surveillance | Exchanges can kill a specific algo ID; exchanges surveil algo orders; brokers monitor APIs and handle grievances (para II(c), IV(a)). | Our own kill switch remains necessary. Broker and exchange controls are added on top. |
| Exchange modalities | Exchanges issue operational modalities, FAQs, registration SOPs and turnaround times (para IV(c)-(d)). | **Not retrieved** (NSE pages refused automated fetches). Obtain them before any live review. |

## 2. Still to verify (owner / Sharekhan / exchange)

- [ ] Current NSE/BSE implementation standards and FAQs for the framework, and any later SEBI amendment or extension.
- [ ] The numeric orders-per-second threshold, and whether Sharekhan requires registration of self-developed algos below it.
- [ ] Sharekhan's static-IP registration procedure: how many IPs, how to change them, lead time.
- [ ] How the exchange algo ID is attached to orders sent through Sharekhan's API (there is no field in the documented order payload).
- [ ] Whether Sharekhan's login flow is the OAuth + 2FA access the circular requires, and whether a daily login remains mandatory.
- [ ] Broker-side kill switch / per-client limits Sharekhan applies to API clients.

## 3. Broker / account

- [ ] Account is active, KYC complete, API key + Secret Key issued to the owner.
- [ ] Redirect URL registration procedure confirmed (Blocker B-04).
- [ ] Segments/exchanges enabled (`exchanges` in profile response) — MVP needs `NC` (and optionally `BC`) only.
- [ ] Product types for intraday vs delivery confirmed (B-02); auto square-off time confirmed.
- [ ] Real rate limits confirmed in writing (B-03).
- [ ] Terms of use for API **and for redistributing/storing market data** reviewed (internal personal use vs multi-user).
- [ ] Written answers on discrepancies D1–D8.

## 4. Data providers

- [ ] Licence for intraday historical data beyond Sharekhan's 7-day window (B-06).
- [ ] Licence for fundamentals, corporate actions, announcements; storage/derivative-use rights.
- [ ] No scraping of sources whose terms prohibit it.

## 5. Product & disclosure

- [ ] No guaranteed-return language anywhere in UI/docs.
- [ ] If used by anyone other than the account owner: assess investment-adviser / research-analyst obligations before release.
- [ ] Tax/levy model reviewed (STT, exchange transaction charges, SEBI fees, stamp duty, GST, DP charges) with current rates and sources.

## 6. Live-readiness gate (Phase 10)

- [ ] ≥ agreed paper-trading observation period completed with review (PAPER_TRADING.md).
- [ ] End-to-end tests incl. failure injection pass (TEST_PLAN.md), results recorded.
- [ ] Reconciliation clean for the observation period.
- [ ] Monitoring, alerting, runbook, rollback plan in place (OPERATIONS_RUNBOOK.md).
- [ ] Risk limits set and signed off by owner.
- [ ] Explicit written live-activation authorisation recorded in the system.
