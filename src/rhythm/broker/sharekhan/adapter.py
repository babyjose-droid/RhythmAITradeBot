"""Read-only Sharekhan adapter implementing ``BrokerReadAdapter``.

The adapter holds no credentials of its own: an injected ``session_provider`` returns the
current access token (decrypted in memory by the API layer) or None when nobody has signed
in to Sharekhan in the last 24 h. It never logs or returns the token or the customer ID.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlencode

import websockets

from rhythm.broker.base import (
    BrokerAuthError,
    BrokerOrderRecord,
    BrokerProfile,
    BrokerResponseError,
    BrokerUnavailableError,
    FundsSnapshot,
    HoldingRecord,
    PositionRecord,
)
from rhythm.broker.sharekhan import readparsers as rp
from rhythm.broker.sharekhan.client import RawResponse, SharekhanReadClient
from rhythm.broker.sharekhan.parsers import EXCHANGE_TO_SHAREKHAN, parse_scrip_master
from rhythm.core.clock import Clock
from rhythm.core.market import DataOrigin, Exchange
from rhythm.instruments.types import ScripRecord


@dataclass(frozen=True)
class ActiveSession:
    access_token: str = field(repr=False)
    customer_id: str = field(repr=False)
    account_ref: str  # masked, safe to show
    exchanges: tuple[str, ...] = ()


SessionProvider = Callable[[], ActiveSession | None]


def mask(customer_id: str) -> str:
    return f"****{customer_id[-3:]}" if len(customer_id) > 3 else "****"


class SharekhanBroker:
    name = "sharekhan"
    origin = DataOrigin.SHAREKHAN

    def __init__(self, client: SharekhanReadClient, session: SessionProvider, clock: Clock) -> None:
        self.client = client
        self._session = session
        self._clock = clock
        self.last_raw: RawResponse | None = None  # for the verification run (shapes only)

    def _s(self) -> ActiveSession:
        s = self._session()
        if s is None:
            raise BrokerAuthError("not signed in to Sharekhan: an owner must log in (valid 24 h)")
        return s

    def _keep(self, raw: RawResponse) -> RawResponse:
        self.last_raw = raw
        return raw

    def get_profile(self) -> BrokerProfile:
        s = self._s()
        return BrokerProfile(
            "sharekhan", s.account_ref, s.exchanges, self.origin, self._clock.now()
        )

    def get_funds(self, exchange: Exchange) -> FundsSnapshot:
        s = self._s()
        raw = self._keep(
            self.client.funds(s.access_token, EXCHANGE_TO_SHAREKHAN[exchange], s.customer_id)
        )
        return rp.parse_funds(raw.data, exchange, self._clock.now())

    def get_holdings(self) -> Sequence[HoldingRecord]:
        s = self._s()
        raw = self._keep(self.client.holdings(s.access_token, s.customer_id))
        return rp.parse_holdings(raw.data, self._clock.now()).records

    def get_positions(self) -> Sequence[PositionRecord]:
        s = self._s()
        raw = self._keep(self.client.positions(s.access_token, s.customer_id))
        return rp.parse_positions(raw.data, self._clock.now()).records

    def get_day_orders(self) -> Sequence[BrokerOrderRecord]:
        s = self._s()
        raw = self._keep(self.client.day_orders(s.access_token, s.customer_id))
        return rp.parse_day_orders(raw.data, self._clock.now()).records

    def get_scrip_master(self, exchange: Exchange) -> Sequence[ScripRecord]:
        s = self._s()
        raw = self._keep(self.client.scrip_master(s.access_token, EXCHANGE_TO_SHAREKHAN[exchange]))
        return parse_scrip_master(raw.http_status, raw.body, exchange)

    def get_candles(self, exchange: Exchange, scrip_code: str, interval: str) -> list[rp.Candle]:
        s = self._s()
        raw = self._keep(
            self.client.historical(
                s.access_token, EXCHANGE_TO_SHAREKHAN[exchange], scrip_code, interval
            )
        )
        return rp.parse_candles(raw.data)


# --- price stream probe (read-only; subscribes to LTP only) -------------------------------


@dataclass(frozen=True)
class StreamProbe:
    connected: bool
    messages: int
    first_message_ms: int | None
    message_shape: dict[str, object] | None
    error: str | None


async def _probe(url: str, keys: Sequence[str], seconds: float) -> StreamProbe:
    start = time.monotonic()
    count = 0
    first: int | None = None
    shape: dict[str, object] | None = None
    try:
        async with websockets.connect(url, open_timeout=10, close_timeout=2) as ws:
            await ws.send(
                json.dumps({"action": "subscribe", "key": ["feed"], "value": [""]})
            )  # feed only, no order acks
            await ws.send(json.dumps({"action": "feed", "key": ["ltp"], "value": list(keys)}))
            deadline = start + seconds
            while (left := deadline - time.monotonic()) > 0:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=left)
                except TimeoutError:
                    break
                count += 1
                if first is None:
                    first = int((time.monotonic() - start) * 1000)
                    try:
                        shape = rp.shape(json.loads(msg))
                    except (ValueError, TypeError):
                        shape = {"kind": "non-json"}
        return StreamProbe(True, count, first, shape, None)
    except Exception as exc:  # report, never raise: this is a diagnostic
        return StreamProbe(count > 0, count, first, shape, type(exc).__name__)


def probe_stream(
    stream_url: str, access_token: str, api_key: str, feed_keys: Sequence[str], seconds: float
) -> StreamProbe:
    """Connect, subscribe to LTP for ``feed_keys`` (e.g. 'NC2885'), count messages, disconnect."""
    if not 1 <= len(feed_keys) <= 5:
        raise ValueError("probe 1-5 instruments")
    seconds = max(1.0, min(seconds, 20.0))
    url = f"{stream_url}?{urlencode({'ACCESS_TOKEN': access_token, 'API_KEY': api_key})}"
    return asyncio.run(_probe(url, feed_keys, seconds))


__all__ = [
    "ActiveSession",
    "BrokerResponseError",
    "BrokerUnavailableError",
    "SharekhanBroker",
    "StreamProbe",
    "mask",
    "probe_stream",
]
