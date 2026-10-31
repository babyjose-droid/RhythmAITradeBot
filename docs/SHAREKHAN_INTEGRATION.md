# Sharekhan Integration — Verified Capabilities, Gaps and Blockers

**Status:** Phase 0 research. No code calls Sharekhan yet. No credentials have been used.
**Accessed:** 2026-10-31 (IST), by rendering the official documentation pages and reading the official SDK source.
**Rule:** Anything not listed as *Confirmed* below must be treated as unknown. The adapter will only implement confirmed behaviour, behind a typed interface, and will be tested against mocks until a human-supervised read-only verification against the live API is approved.

## 1. Sources reviewed

| # | Source | Type | Notes |
|---|--------|------|-------|
| S1 | https://www.sharekhan.com/trading-api/documentation (sub-pages: `/overview`, `/libraries-sdk`, `/login-and-user-details`, `/order-and-trades`, `/holdings-and-limits`, `/funds`, `/historical-api`, `/script-master`, `/web-socket-api`, `/response-structure`, `/data-type`, `/error-code`) | Official docs | Content is JS-rendered; plain HTTP fetch returns navigation only. Read via a rendered browser. |
| S2 | https://www.sharekhan.com/trading-api | Official marketing/overview | Rate-limit and pricing statements. |
| S3 | https://www.sharekhan.com/faq/API | Official FAQ | Historical-data depth, streaming limits, pricing. |
| S4 | https://github.com/Sharekhan-API/shareconnectpython (commit `a81c80f`, 2024-04-26, setup.py version `1.0.0.11`) | Official SDK (linked from S1/S2) | Source read to cross-check routes. `https://github.com/Sharekhan` (the org in the blueprint) has **no public repositories**; SDKs live under `Sharekhan-API`. |

Where S1 and S4 disagree, the discrepancy is recorded in §9 and treated as a blocker until confirmed with Sharekhan (support: `skapi@sharekhan.com`, per S2).

## 2. General conventions (Confirmed — S1 Overview, Response Structure, Data Type, Error Codes)

- Base URL: `https://api.sharekhan.com/skapi/`
- "HTTPS-REST", JSON requests and responses; **only GET and POST are allowed**.
- Required headers for customers: `access-token`, `api-key`, `Content-Type: application/json`. Partners/vendors additionally send `vendor-key`.
- Success envelope: `{"status": 200, "message": "...", "timestamp": "yyyy-MM-dd'T'HH:mm:ssXXX", "data": ...}`.
- Error envelope: `{"status": 5xx|4xx, "message": "...", "errorType": "..."}`.
- Status codes: `200`, `204` (no records, e.g. empty order history/chart), `400` bad/missing params, `403` token expired/access denied (regenerate token), `404` not found, `429` rate limited, `500` general, `502` service error / RMS down.
- Error types: Token, Input, Service, General, User (account-related).
- Prices should be sent **as strings** to avoid float errors. Dates in requests: `DD/MM/YYYY`.
- Exchange codes: `NC` = NSE equity, `BC` = BSE equity, `NF` = NSE F&O, `RN` = NSE currency, `MX` = MCX F&O.

## 3. Authentication (Confirmed — S1 Login & Profile Data; S4)

1. Redirect the user's browser to
   `GET https://api.sharekhan.com/skapi/auth/login.html?api_key={api_key}&state={state}[&vendor_key={vendor_key}][&version_id=1005]`
   The user logs in **on Sharekhan's page** (we never see or store their password).
2. Sharekhan redirects to the app's redirect URL with `?request_token=...&state=...`.
3. The request token decrypts (AES-256-GCM, key = the customer's 32-byte **Secret Key**, 16-byte all-zero IV/nonce, 128-bit tag, as specified by the broker) to `RequestId|CustomerId`. Swap to `CustomerId|RequestId` and re-encrypt with the same scheme. With `version_id=1005` use URL-safe Base64; without it, standard Base64.
4. `POST https://api.sharekhan.com/skapi/services/access/token` with JSON `{"apiKey", "requestToken" (manipulated), "state", ["vendorKey"], ["versionId": 1005]}`.
5. Response `data` contains `token` (JWT), `fullName`, `customerId`, `loginId`, `broker`, `exchanges` (e.g. `["BC","NC","NF","RN","MX"]`), `state`.
6. **Token validity: 24 hours.** "When the Token expires, the user must generate a new Access Token." **No refresh-token flow is documented.**
7. Logout: `https://api.sharekhan.com/skapi/services/logout/{customerid}` (HTTP method not stated).

Design consequences:
- Login is an **interactive, daily, human step**. We will not automate credential entry, TOTP or the login page. The system must degrade to RESEARCH_ONLY/fail-closed when no valid token exists.
- `state` must be a cryptographically random, single-use CSRF value bound to the user session (S1 shows the constant `12345` in samples; the SDK hardcodes it — we will not).
- The fixed all-zero GCM nonce is the broker's protocol, not our choice. It is recorded as a security observation (SECURITY_MODEL.md). We only use the Secret Key for this exchange and keep it in a secrets store.
- How the redirect URL is registered against an API key is **not documented** (Blocker B-04).

## 4. Orders (Confirmed — S1 Order & Trades)

- `POST https://api.sharekhan.com/skapi/services/orders` — new, modify and cancel, distinguished by `requestType` = `NEW` | `MODIFY` | `CANCEL`.
- Request fields: `customerId` (long), `scripCode` (int), `tradingSymbol`, `exchange` (`NC/BC/NF/RN/MX`), `transactionType` (table: `B/S/SAM`; sample comment: `B, S, BM, SM, SAM`), `quantity`, `disclosedQty`, `price` (string; **"0" for market orders**), `triggerPrice` (string), `rmsCode` (`ANY` for new; the specific code returned for modify/cancel), `afterHour` (`Y` = AMO), `orderType` (only `NORMAL` documented), `channelUser` (login ID), `validity` (`GFD` / `MyGTD` / `IOC`), `gtdd` (date for MyGTD), `requestType`, `productType` (`INVESTMENT`, `BIGTRADE`, `BIGTRADE+`).
- Modify/cancel add `orderId`, `executedQty`; for partially executed orders `quantity` semantics are documented (open vs executed quantity).
- Response: `data.orderId`, `data.rmscode`, `data.errormsg`, `data.serverTime`. The docs state the order is "pushed to RMS"; **a 200 response means accepted by Sharekhan's RMS for processing, not executed.** Modify/cancel responses return `orderId: "0"`, `rmscode: "null"`.
- Cancel by order ID only: `GET https://api.sharekhan.com/skapi/services/cancelOrder/{orderid}`.
- Derivatives fields (`instrumentType`, `optionType`, `strikePrice`, `expiry`) exist; **out of scope** for MVP (blueprint non-goal).
- FAQ (S3): "With the API, you can place Intraday and Delivery orders for now." S2: "Advanced Order Placement — Coming Soon!".

**Not documented / unclear:**
- **No client order ID / idempotency key field.** Duplicate prevention must be ours (see RISK_MANAGEMENT.md §6). A timed-out POST must be resolved by reading the order book, never by blind retry.
- Which `productType` is intraday vs delivery for cash equity. The C# sample comment says "For Equity Exchange - CNC (Normal), For F&O - CNF(normal), MIS, MIS+", which does not match the documented enum (Blocker B-02).
- Meaning of `BM`, `SM`, `SAM` transaction types.
- Stop-loss order semantics (whether `triggerPrice` alone creates an SL/SL-M order), bracket/cover orders, tick-size/price-band rejection codes.
- Market-order protection, auto square-off time for intraday products.

## 5. Reports, positions, holdings, funds (Confirmed endpoints — S1; field meanings partly undocumented)

| Purpose | Method + path (S1) | Notes |
|---|---|---|
| Orders for the day | `GET /skapi/services/reports/{customerId}` | Fields include `orderStatus` (e.g. `FullyExecuted`, `Pending`, `InProcess`), `requestStatus`, `orderQty`, `execQty`, `execPrice`, `exchOrderId`, `rmsCode`, `lastModTime`. Full status enum **not documented**. |
| Positions ("Retrieves all positions") | `GET /skapi/services/trades/{customerId}` | Response has `buyQty`, `sellQty`, `netQty`, `avgPrice`, `productType`, MTM fields. |
| History of one order | `GET /skapi/services/reports/{exchange}/{customerId}/{orderId}` | Sequence of states with `exchAckDateTime`. |
| Trades (fills) of one order | Docs table: `GET /skapi/services/reports/{exchange}/{customerId}/{orderId}/trades`; SDK: `/skapi/services/orders/{exchange}/{customerId}/{orderId}/trades` | **Conflict** (§9). Fields: `tradeId`, `internalTradeId`, `tradeQty`, `tradePrice`, `tradeDateTime`, `exchOrderId`. |
| Holdings | `GET /skapi/services/holdings/{customerId}` | Fields `dp`, `aval`, `pledge`, `cncqty`, `holdPrice`, `tradingAllowed`, etc.; most field meanings are blank in docs. |
| Funds / limits | `GET /skapi/services/limitstmt/{exchange}/{customerId}` | `currentCashBalance`, `nonCashLimit`, `intradayMarginCash`, `holdFunds`, etc. Docs: "MTM and Total Available Limit are calculated from the user's side" — **we must compute available funds ourselves**; formula not documented (Blocker B-05). |

## 6. Market data

**Historical candles (Confirmed — S1, S3):**
- `GET /skapi/services/historical/{exchange}/{scripcode}/{interval}`
- Intervals: `daily, weekly, monthly, quarterly, halfyearly, yearly, 1minute, 3minute, 5minute, 10minute, 15minute, 30minute, 60minute, 90minute`.
- Fields: `open, high, low, close, qty, tradedValue, tradeDate (DD/MM/YYYY), tradeTime`.
- **No date-range parameters.** FAQ: EOD data "since inception"; **intraday data only for the last 7 days**; data is **not adjusted for corporate actions**.

**Scrip master (Confirmed — S1):** `GET /skapi/services/master/{exchange}`; "Only current day active scrips are provided." Fields: `scripCode, tradingSymbol, tickSize, instType, lotSize, companyName, indices, groupName, industry, isinCode, expiry, strike, optionType`.

**WebSocket (Confirmed — S1, S3, S4):**
- URL: `wss://stream.sharekhan.com/skstream/api/stream?ACCESS_TOKEN={access_token}&API_KEY={api_key}` (SDK omits `API_KEY` — §9).
- Connect response `status: 100`, `data: "Connected <session-uuid>"`.
- Subscribe modules: `{"action":"subscribe","key":["feed","ack"],"value":[""]}`.
- Order acknowledgements: `{"action":"ack","key":[""],"value":[customerId]}` → messages with `SharekhanOrderID`, `ExchangeOrderID`, `AckState` (e.g. `NewOrderConfirmation`), `OrderQty`, `RemainingQty`, `TradeQty`, `TradePrice`, `TradeID`, `ExchangeDateTime`.
- Feed: `{"action":"feed","key":["ltp"|"depth"|"full"],"value":["NC2885"]}` (symbol = exchange code + scrip code). Unsubscribe: `{"action":"unsubscribe","key":["feed"],"value":["NC22,..."]}`.
- Feed fields include `ltp, ltt, bidPrice/Qty, offPrice/Qty, open, high, low, close, avgPrice, qty, upperCkt, lowerCkt, yrHigh, yrLow, lastUpdatedTime`.
- Max **1000 symbols per connection** (S3). Top-5 depth (S2). SDK sends text `"ping"` heartbeats.
- **Not documented:** reconnect semantics, message sequencing/gap detection, full `AckState` enum, timezone of `ltt` (assumed IST — to verify), entitlement differences between accounts.

**Not available from Sharekhan API (per docs reviewed):** company fundamentals, financial statements, valuation data, corporate-action calendars, news/announcements, index constituents history, delisted-instrument history. Long-term and swing research need separate licensed sources (Blocker B-06).

## 7. Limits, environments, pricing

- S2: "Order Placement: Up to 30 requests per second"; "Order Execution: Up to 25 ms for a trade". S3 rate-limit answer is a placeholder ("XX orders per second") — **effective limits unconfirmed**; `429` exists. Data-endpoint limits not documented. We will self-throttle well below 30/s and back off on 429.
- **No sandbox / UAT environment is documented.** Every real call is production. Paper trading is therefore fully simulated inside our system; the adapter is developed against recorded/mock fixtures.
- S3: API is free; normal brokerage applies; available to all active customers; API key issued on sign-up.

## 8. Regulatory items not covered by Sharekhan docs

Sharekhan's pages reviewed contain no statement on static-IP whitelisting, algo registration/IDs, order tagging, or SEBI's retail algo framework. SEBI circular *"Safer participation of retail investors in Algorithmic trading"*, 04-Feb-2025, No. SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 exists (title/date/number confirmed at sebi.gov.in); its body and the exchanges' implementation standards could not be retrieved in this session. See COMPLIANCE_CHECKLIST.md — **must be verified with Sharekhan before any live use** (Blocker B-01).

## 9. Discrepancies between official sources (must be resolved with Sharekhan)

| ID | Topic | S1 docs | S4 SDK |
|---|---|---|---|
| D1 | Trades of an order | `/services/reports/{exchange}/{customerId}/{orderId}/trades` (table); section body shows `.../reports/exchange}/{customerId}/{orderId}` | `/services/orders/{exchange}/{customerId}/{orderId}/trades` |
| D2 | "Executed trades for the day" section | Shows URL `/services/reports/{customerId}` but response `message: "trades_history"` with position fields | `trades(customerId)` → `/services/trades/{customerId}` |
| D3 | "History of an order" section | Shows URL `/services/trades/{customerId}` | `/services/reports/{exchange}/{customerId}/{orderId}` |
| D4 | Auth header | `access-token`, `api-key` headers | Same, **plus** `Authorization: <token>` |
| D5 | WebSocket URL | includes `API_KEY` query param | omits `API_KEY` |
| D6 | GET params | Logout section: GET/DELETE params as query, POST/PUT as form-encoded | Sends JSON body for POST; JSON-serialised dict as query for GET |
| D7 | transactionType | `B/S/SAM` | `B, S, BM, SM, SAM` |
| D8 | productType | `INVESTMENT, BIGTRADE, BIGTRADE+` | Sample comments also mention `CNC, CNF, MIS, MIS+`, `INV, BT, BT+` |

## 10. SDK assessment — we will NOT depend on the official Python SDK

Read from source (S4): it calls `requests.packages.urllib3.disable_warnings()`, connects the WebSocket with `sslopt={"cert_reqs": ssl.CERT_NONE}` (**TLS certificate verification disabled**), prints to stdout, hardcodes `state=12345`, pins 2023-era dependencies (e.g. `cryptography==39.0.2`, `requests==2.28.2`), and was last updated 2024-04-26. We will write a thin, typed, TLS-verifying adapter (`httpx` + `websockets`) implementing only confirmed routes, using the SDK only as a reference.

## 11. Adapter plan (interface first, mock first)

```
BrokerAdapter (Protocol)
  auth:        build_login_url(state) / exchange_request_token(token, state) -> BrokerSession
  read-only:   get_profile, get_funds(exchange), get_holdings, get_positions,
               get_day_orders, get_order_history(order_id), get_order_trades(order_id)
  reference:   get_scrip_master(exchange), get_historical(exchange, scrip, interval)
  streaming:   stream_quotes(symbols) / stream_order_acks()
  write:       place_order / modify_order / cancel_order   # LIVE only; not implemented before Phase 10
```

Implementations: `MockBrokerAdapter` (deterministic fixtures, failure injection), `PaperBrokerAdapter` (internal simulator), `SharekhanAdapter` (read-only methods first; write methods raise `LiveTradingDisabled` until the Phase 10 gate). Every response is persisted raw (redacted) with request time, response time and source.

## 12. Verification still required (human-supervised, read-only, after approval)

1. One login + token exchange using the user's own API key on a registered redirect URL.
2. Read-only calls: profile, funds, holdings, positions, day orders, scrip master (NC), historical (NC, daily + 5minute).
3. WebSocket connect + one `ltp` subscription during market hours; record message shapes and timestamps.
4. Resolve D1–D8 against observed responses and/or written confirmation from Sharekhan.
No order endpoint will be called in any of these steps.
