"""Read-only Sharekhan client: token transform, documented reads, and no order surface."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from rhythm.broker.base import BrokerAuthError, BrokerReadAdapter, BrokerResponseError
from rhythm.broker.sharekhan import readparsers as rp
from rhythm.broker.sharekhan import token as tok
from rhythm.broker.sharekhan.adapter import ActiveSession, SharekhanBroker, mask
from rhythm.broker.sharekhan.client import READ_PATHS, SharekhanReadClient
from rhythm.core.clock import FixedClock
from rhythm.core.market import DataOrigin, Exchange
from tests import sharekhan_fake as fake
from tests.conftest import make_settings

NOW = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
ORDER_WORDS = ("place", "modify", "cancel", "submit", "exit", "convert", "order_place")


def client(f: fake.FakeSharekhan, version: int | None = 1005) -> SharekhanReadClient:
    return SharekhanReadClient(
        api_key=fake.API_KEY,
        secret_key=fake.SECRET,
        base_url="https://api.sharekhan.com/skapi",
        version_id=version,
        transport=f.transport(),
    )


def broker(f: fake.FakeSharekhan, signed_in: bool = True) -> SharekhanBroker:
    s = ActiveSession(fake.ACCESS, fake.CUSTOMER, mask(fake.CUSTOMER), ("NC", "BC"))
    return SharekhanBroker(client(f), lambda: s if signed_in else None, FixedClock(NOW))


@pytest.mark.parametrize("urlsafe", [True, False])
def test_token_round_trip_and_swap(urlsafe: bool) -> None:
    t = fake.request_token(urlsafe)
    rt = tok.decrypt(t, fake.SECRET, urlsafe=urlsafe)
    assert (rt.request_id, rt.customer_id) == (fake.REQUEST_ID, fake.CUSTOMER)
    swapped, cust = tok.manipulate(t, fake.SECRET, urlsafe=urlsafe)
    assert cust == fake.CUSTOMER
    back = tok.decrypt(swapped, fake.SECRET, urlsafe=urlsafe)
    assert (back.request_id, back.customer_id) == (fake.CUSTOMER, fake.REQUEST_ID)


def test_token_errors() -> None:
    t = fake.request_token()
    with pytest.raises(BrokerAuthError, match="did not decrypt"):
        tok.decrypt(t, "x" * 32)
    with pytest.raises(BrokerAuthError, match="32 bytes"):
        tok.decrypt(t, "short")
    with pytest.raises(BrokerAuthError, match="RequestId"):
        tok.decrypt(tok.encrypt("no-separator", fake.SECRET), fake.SECRET)


def test_login_url_and_exchange() -> None:
    f = fake.FakeSharekhan()
    c = client(f)
    q = parse_qs(urlparse(c.login_url("st8")).query)
    assert q == {"api_key": [fake.API_KEY], "state": ["st8"], "version_id": ["1005"]}
    g = c.exchange_request_token(fake.request_token(), "st8")
    assert (g.access_token, g.customer_id, g.exchanges) == (
        fake.ACCESS,
        fake.CUSTOMER,
        ("NC", "BC"),
    )
    assert "fake-access" not in repr(ActiveSession(g.access_token, g.customer_id, "****567"))


def test_reads_parse_documented_shapes() -> None:
    f = fake.FakeSharekhan()
    b = broker(f)
    assert isinstance(b, BrokerReadAdapter)
    p = b.get_profile()
    assert p.account_ref == "****567" and fake.CUSTOMER not in p.account_ref
    funds = b.get_funds(Exchange.NSE)
    assert funds.cash_balance == Decimal("125000.50") and funds.origin is DataOrigin.SHAREKHAN
    (h,) = b.get_holdings()
    assert (h.symbol, h.quantity, h.average_price) == ("TCS", 10, Decimal("3500.00"))
    pos = b.get_positions()
    assert [(x.symbol, x.net_quantity) for x in pos] == [("RELIANCE", 5)]  # NF skipped
    (o,) = b.get_day_orders()
    assert (o.status, o.filled_quantity) == ("FullyExecuted", 5)
    assert [s.symbol for s in b.get_scrip_master(Exchange.NSE)] == ["RELIANCE", "TCS"]
    candles = b.get_candles(Exchange.NSE, "2885", "daily")
    assert [c.close for c in candles] == [Decimal(2400), Decimal(2415)]


def test_only_gets_to_read_paths_plus_token_post() -> None:
    f = fake.FakeSharekhan()
    c = client(f)
    c.exchange_request_token(fake.request_token(), "s")
    b = broker(f)
    b.get_funds(Exchange.NSE), b.get_holdings(), b.get_positions(), b.get_day_orders()
    b.get_scrip_master(Exchange.NSE), b.get_candles(Exchange.NSE, "2885", "daily")
    posts = [r for r in f.requests if r.method != "GET"]
    assert [(r.method, r.url.path) for r in posts] == [("POST", "/skapi/services/access/token")]
    for r in f.requests:
        if r.method == "GET":
            assert r.url.path.removeprefix("/skapi").startswith(READ_PATHS)
            assert r.url.scheme == "https"


def test_no_order_surface() -> None:
    for cls in (SharekhanReadClient, SharekhanBroker):
        names = [n for n, _ in inspect.getmembers(cls) if not n.startswith("__")]
        assert not [n for n in names if any(w in n.lower() for w in ORDER_WORDS)], cls
    src = inspect.getsource(SharekhanReadClient)
    assert ".post(" in src and src.count(".post(") == 1  # the token exchange only
    for verb in (".put(", ".delete(", ".patch(", ".request("):
        assert verb not in src


def test_errors_are_explicit() -> None:
    f = fake.FakeSharekhan()
    with pytest.raises(BrokerAuthError, match="not signed in"):
        broker(f, signed_in=False).get_holdings()
    f.expire_reads = True
    with pytest.raises(BrokerAuthError, match="403"):
        broker(f).get_holdings()
    with pytest.raises(ValueError, match="interval"):
        broker(fake.FakeSharekhan()).get_candles(Exchange.NSE, "2885", "2minute")
    with pytest.raises(ValueError, match="segment"):
        client(fake.FakeSharekhan()).holdings(fake.ACCESS, "../x")
    with pytest.raises(BrokerResponseError, match="symbol"):
        rp.parse_holdings([{"qty": 1}], NOW)
    with pytest.raises(BrokerResponseError, match="sanity"):
        rp.parse_candles([{"open": 1, "high": 1, "low": 2, "close": 1, "tradeDate": "04/11/2026"}])


def test_shape_never_contains_values() -> None:
    sh = rp.shape(fake.HOLDINGS)
    assert sh["count"] == 1 and sh["fields"]["holdPrice"] == ["string"]
    flat = repr(sh)
    for v in ("TCS", "3500", "INE467B01029"):
        assert v not in flat


def _sk(**kw: object) -> object:
    base: dict[str, object] = {
        "broker_adapter": "sharekhan",
        "sharekhan": {"api_key": fake.API_KEY, "secret_key": fake.SECRET},
        "auth": {"enabled": True, "secret_key": "k" * 40},
    }
    base.update(kw)
    return make_settings(**base)


def test_settings_guard_sharekhan() -> None:
    s = _sk()
    assert s.sharekhan.base_url.startswith("https://")  # type: ignore[attr-defined]
    assert fake.SECRET not in repr(s)
    with pytest.raises(ValueError, match="SECRET_KEY"):
        _sk(sharekhan={"api_key": fake.API_KEY})
    with pytest.raises(ValueError, match="32-character"):
        _sk(sharekhan={"api_key": fake.API_KEY, "secret_key": "short"})
    with pytest.raises(ValueError, match="needs sign-in"):
        _sk(auth={"enabled": False})
    with pytest.raises(ValueError, match="https"):
        _sk(sharekhan={"api_key": "a", "secret_key": fake.SECRET, "base_url": "http://x"})
