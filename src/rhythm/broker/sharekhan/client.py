"""Read-only Sharekhan HTTP client (documented endpoints only; SHAREKHAN_INTEGRATION.md).

What this client can do: build the login URL, exchange a request token for a 24 h access
token, and issue GET requests to the documented read endpoints. That is the whole surface.

There is no order, modify, cancel or logout-of-other-sessions call and no generic "request"
method: every outgoing request goes through ``_get`` (HTTP GET) except the single access-token
POST in ``exchange_request_token``. tests/unit/test_sharekhan_client.py asserts this.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from rhythm.broker.base import BrokerResponseError, BrokerUnavailableError
from rhythm.broker.sharekhan import token as tok
from rhythm.broker.sharekhan.parsers import check_envelope

READ_PATHS = (
    "/services/limitstmt/",
    "/services/holdings/",
    "/services/trades/",
    "/services/reports/",
    "/services/master/",
    "/services/historical/",
)
TOKEN_PATH = "/services/access/token"  # noqa: S105 - a URL path, not a secret
LOGIN_PATH = "/auth/login.html"
HISTORICAL_INTERVALS = frozenset(
    {
        "daily",
        "weekly",
        "monthly",
        "quarterly",
        "halfyearly",
        "yearly",
        "1minute",
        "3minute",
        "5minute",
        "10minute",
        "15minute",
        "30minute",
        "60minute",
        "90minute",
    }
)


@dataclass(frozen=True)
class SessionGrant:
    """Result of a successful token exchange. ``access_token`` must be sealed before storage."""

    access_token: str
    customer_id: str
    full_name: str | None
    login_id: str | None
    exchanges: tuple[str, ...]


@dataclass(frozen=True)
class RawResponse:
    path_template: str
    http_status: int
    latency_ms: int
    body: Mapping[str, Any] | None
    data: Any = field(default=None)


class SharekhanReadClient:
    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        base_url: str,
        vendor_key: str | None = None,
        version_id: int | None = 1005,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._vendor_key = vendor_key
        self._version_id = version_id
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=timeout_seconds, transport=transport, verify=True, follow_redirects=False
        )

    def close(self) -> None:
        self._http.close()

    # --- login -------------------------------------------------------------------------------
    def login_url(self, state: str) -> str:
        params: dict[str, str] = {"api_key": self._api_key, "state": state}
        if self._vendor_key:
            params["vendor_key"] = self._vendor_key
        if self._version_id is not None:
            params["version_id"] = str(self._version_id)
        return f"{self._base}{LOGIN_PATH}?{urlencode(params)}"

    def exchange_request_token(self, request_token: str, state: str) -> SessionGrant:
        urlsafe = self._version_id == 1005
        manipulated, customer_id = tok.manipulate(request_token, self._secret_key, urlsafe=urlsafe)
        payload: dict[str, Any] = {
            "apiKey": self._api_key,
            "requestToken": manipulated,
            "state": state,
        }
        if self._vendor_key:
            payload["vendorKey"] = self._vendor_key
        if self._version_id is not None:
            payload["versionId"] = self._version_id
        try:
            r = self._http.post(
                f"{self._base}{TOKEN_PATH}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise BrokerUnavailableError(
                f"Sharekhan token exchange failed: {type(exc).__name__}"
            ) from exc
        data = check_envelope(r.status_code, _json(r))
        if not isinstance(data, Mapping) or not data.get("token"):
            raise BrokerResponseError("token response has no data.token")
        exch = data.get("exchanges") or ()
        return SessionGrant(
            access_token=str(data["token"]),
            customer_id=str(data.get("customerId") or customer_id),
            full_name=(str(data["fullName"]) if data.get("fullName") else None),
            login_id=(str(data["loginId"]) if data.get("loginId") else None),
            exchanges=tuple(str(e) for e in exch) if isinstance(exch, list | tuple) else (),
        )

    # --- reads (GET only) --------------------------------------------------------------------
    def funds(self, access_token: str, exchange_code: str, customer_id: str) -> RawResponse:
        return self._get(
            access_token,
            "/services/limitstmt/{exchange}/{customerId}",
            f"/services/limitstmt/{_seg(exchange_code)}/{_seg(customer_id)}",
        )

    def holdings(self, access_token: str, customer_id: str) -> RawResponse:
        return self._get(
            access_token,
            "/services/holdings/{customerId}",
            f"/services/holdings/{_seg(customer_id)}",
        )

    def positions(self, access_token: str, customer_id: str) -> RawResponse:
        return self._get(
            access_token, "/services/trades/{customerId}", f"/services/trades/{_seg(customer_id)}"
        )

    def day_orders(self, access_token: str, customer_id: str) -> RawResponse:
        return self._get(
            access_token, "/services/reports/{customerId}", f"/services/reports/{_seg(customer_id)}"
        )

    def scrip_master(self, access_token: str, exchange_code: str) -> RawResponse:
        return self._get(
            access_token, "/services/master/{exchange}", f"/services/master/{_seg(exchange_code)}"
        )

    def historical(
        self, access_token: str, exchange_code: str, scrip_code: str, interval: str
    ) -> RawResponse:
        if interval not in HISTORICAL_INTERVALS:
            raise ValueError(f"unsupported interval {interval!r}")
        return self._get(
            access_token,
            "/services/historical/{exchange}/{scripcode}/{interval}",
            f"/services/historical/{_seg(exchange_code)}/{_seg(scrip_code)}/{interval}",
        )

    def _get(self, access_token: str, template: str, path: str) -> RawResponse:
        if not path.startswith(READ_PATHS):  # defence in depth: only documented read paths
            raise ValueError(f"not a read path: {template}")
        headers = {
            "access-token": access_token,
            "api-key": self._api_key,
            "Content-Type": "application/json",
        }
        start = time.monotonic()
        try:
            r = self._http.get(f"{self._base}{path}", headers=headers)
        except httpx.HTTPError as exc:
            raise BrokerUnavailableError(f"Sharekhan GET failed: {type(exc).__name__}") from exc
        latency = int((time.monotonic() - start) * 1000)
        body = _json(r)
        return RawResponse(
            template, r.status_code, latency, body, check_envelope(r.status_code, body)
        )


def _seg(value: str) -> str:
    if not value or "/" in value or ".." in value:
        raise ValueError("invalid path segment")
    return quote(value, safe="")


def _json(r: httpx.Response) -> Mapping[str, Any] | None:
    if r.status_code == 204 or not r.content:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    return body if isinstance(body, Mapping) else None
