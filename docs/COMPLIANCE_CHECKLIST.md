# Compliance Checklist

**Status:** Phase 0. This is an engineering checklist, **not legal advice**. Every item must be confirmed against current primary sources and with Sharekhan before any live activation. Live trading stays disabled until every box is ticked by the account owner.

## 1. What was verified in this session (2026-10-31)

- SEBI circular *"Safer participation of retail investors in Algorithmic trading"*, dated 04-Feb-2025, No. SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 — **title, date and number confirmed** at https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html. Body text was not retrievable in this session.
- Sharekhan API docs/FAQ (SHAREKHAN_INTEGRATION.md) contain **no** statement about static IP, algo registration, algo IDs or order tagging.
- Exchange (NSE/BSE) implementation standards: **not retrieved**.

## 2. Items to verify (recalled from general knowledge — UNVERIFIED, do not rely on)

The following are commonly reported elements of the 2025 retail-algo framework. They are listed so they get checked, not as confirmed facts:

- [ ] Whether API orders above an exchange-specified orders-per-second threshold require the algo to be registered with the exchange and carry a unique algo ID.
- [ ] Whether self-developed algos below the threshold (for own/family use) still need registration or tagging through the broker.
- [ ] Whether the broker must whitelist a **static IP** for API order placement and how to register it with Sharekhan (affects hosting — cloud egress IP must be fixed).
- [ ] Whether OAuth-style login / 2FA is mandated (Sharekhan's flow already uses a browser login with request token).
- [ ] White-box vs black-box classification and whether any registration (e.g. Research Analyst) applies — relevant if the platform is ever offered to others.
- [ ] Broker-side kill switch and audit expectations; required order tags/fields.
- [ ] Current effective dates and any extensions.

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
