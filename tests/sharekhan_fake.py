"""A fake Sharekhan API (httpx.MockTransport) shaped like the documented samples.

It records every request so tests can prove the client only ever sends GETs to read paths
plus the single access-token POST. Keys here are test-only values, not real credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from rhythm.broker.sharekhan import token as tok

API_KEY = "test-api-key-not-real"
SECRET = "test-only-secure-key-not-real-32"  # 32 bytes, test only
CUSTOMER = "1234567"
REQUEST_ID = "REQ998877"
ACCESS = "fake-access-token-abc"


def request_token(urlsafe: bool = True) -> str:
    """What Sharekhan would put on the redirect: Enc(RequestId|CustomerId)."""
    return tok.encrypt(f"{REQUEST_ID}|{CUSTOMER}", SECRET, urlsafe=urlsafe)


def ok(data: Any) -> dict[str, Any]:
    return {"status": 200, "message": "ok", "timestamp": "2026-11-04T10:00:00", "data": data}


SCRIPS = [
    {
        "scripCode": 2885,
        "tradingSymbol": "RELIANCE",
        "tickSize": 0.05,
        "instType": "EQ",
        "lotSize": 0,
        "companyName": "Reliance Industries",
        "isinCode": "INE002A01018",
    },
    {
        "scripCode": 11536,
        "tradingSymbol": "TCS",
        "tickSize": 0.05,
        "instType": "EQ",
        "lotSize": 1,
        "companyName": "TCS",
        "isinCode": "INE467B01029",
    },
]
FUNDS = {"currentCashBalance": "125000.50", "nonCashLimit": "0", "holdFunds": "0"}
HOLDINGS = [
    {
        "tradingSymbol": "TCS",
        "exchange": "NC",
        "qty": 10,
        "dp": 10,
        "aval": 10,
        "pledge": 0,
        "cncqty": 0,
        "holdPrice": "3500.00",
        "isin": "INE467B01029",
    }
]
POSITIONS = [
    {
        "tradingSymbol": "RELIANCE",
        "exchange": "NC",
        "buyQty": 5,
        "sellQty": 0,
        "netQty": 5,
        "avgPrice": "2400.10",
        "productType": "INVESTMENT",
    },
    {"tradingSymbol": "NIFTY", "exchange": "NF", "netQty": 50},  # derivative: skipped
]
ORDERS = [
    {
        "orderId": "SK1",
        "exchOrderId": "X1",
        "tradingSymbol": "RELIANCE",
        "exchange": "NC",
        "transactionType": "B",
        "orderQty": 5,
        "execQty": 5,
        "orderStatus": "FullyExecuted",
        "price": "0",
        "execPrice": "2400.10",
    }
]
CANDLES = [
    {
        "open": 2390,
        "high": 2410,
        "low": 2380,
        "close": 2400,
        "qty": 1000,
        "tradedValue": 1,
        "tradeDate": "03/11/2026",
        "tradeTime": "00:00:00",
    },
    {
        "open": 2400,
        "high": 2420,
        "low": 2395,
        "close": 2415,
        "qty": 900,
        "tradedValue": 1,
        "tradeDate": "04/11/2026",
        "tradeTime": "00:00:00",
    },
]


@dataclass
class FakeSharekhan:
    requests: list[httpx.Request] = field(default_factory=list)
    expire_reads: bool = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        p = req.url.path.removeprefix("/skapi")
        if req.method == "POST" and p == "/services/access/token":
            body = json.loads(req.content)
            plain = tok.decrypt(body["requestToken"], SECRET).__dict__
            if body.get("apiKey") != API_KEY or plain != {
                "request_id": CUSTOMER,
                "customer_id": REQUEST_ID,  # swapped by the client
            }:
                return httpx.Response(403, json={"status": 403, "message": "bad token"})
            return httpx.Response(
                200,
                json=ok(
                    {
                        "token": ACCESS,
                        "fullName": "Test User",
                        "customerId": int(CUSTOMER),
                        "loginId": "TU1",
                        "exchanges": ["NC", "BC"],
                    }
                ),
            )
        if req.method != "GET":
            return httpx.Response(405)
        if req.headers.get("access-token") != ACCESS or req.headers.get("api-key") != API_KEY:
            return httpx.Response(403, json={"status": 403})
        if self.expire_reads:
            return httpx.Response(403, json={"status": 403})
        routes = {
            f"/services/limitstmt/NC/{CUSTOMER}": FUNDS,
            f"/services/holdings/{CUSTOMER}": HOLDINGS,
            f"/services/trades/{CUSTOMER}": POSITIONS,
            f"/services/reports/{CUSTOMER}": ORDERS,
            "/services/master/NC": SCRIPS,
            "/services/historical/NC/2885/daily": CANDLES,
        }
        if p in routes:
            return httpx.Response(200, json=ok(routes[p]))
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
