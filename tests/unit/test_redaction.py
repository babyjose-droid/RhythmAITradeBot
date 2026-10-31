from __future__ import annotations

import string

from hypothesis import given
from hypothesis import strategies as st

from rhythm.core.redaction import REDACTED, mask_identifier, redact, redact_text
from tests.fakes import FAKE_JWT

# Synthetic, obviously fake values only.


def test_sensitive_keys_redacted_recursively() -> None:
    data = {
        "api_key": "k1",
        "nested": {"accessToken": "t1", "items": [{"secret_key": "s1"}, {"ok": "visible"}]},
        "Authorization": "Bearer abc",
        "vendor-key": "v1",
        "password": "p1",
        "price": "92.50",
    }
    out = redact(data)
    assert out["api_key"] == REDACTED
    assert out["nested"]["accessToken"] == REDACTED
    assert out["nested"]["items"][0]["secret_key"] == REDACTED
    assert out["nested"]["items"][1]["ok"] == "visible"
    assert out["Authorization"] == REDACTED
    assert out["vendor-key"] == REDACTED
    assert out["password"] == REDACTED
    assert out["price"] == "92.50"
    assert data["api_key"] == "k1"  # input not mutated


def test_jwt_in_free_text_redacted() -> None:
    assert FAKE_JWT not in redact_text(f"got token {FAKE_JWT} ok")


def test_websocket_url_query_token_redacted() -> None:
    url = "wss://stream.example.invalid/skstream/api/stream?ACCESS_TOKEN=abc123&API_KEY=xyz789"
    out = redact_text(url)
    assert "abc123" not in out
    assert "xyz789" not in out
    assert out.startswith("wss://stream.example.invalid/")


def test_login_callback_request_token_and_state_redacted() -> None:
    out = redact_text("https://cb.invalid/?request_token=+AXm/LL==&state=s9")
    assert "+AXm" not in out
    assert "state=s9" not in out


def test_url_credentials_redacted() -> None:
    out = redact_text("postgresql+psycopg://rhythm:pw-123@db:5432/rhythm")
    assert "pw-123" not in out
    assert "rhythm:" in out


def test_bearer_redacted() -> None:
    assert "abc.def" not in redact_text("Authorization: Bearer abc.def")


def test_identifiers_masked() -> None:
    out = redact({"customerId": 12345678, "loginId": "ABCUSER", "tradingSymbol": "ONGC"})
    assert out["customerId"] == "******78"
    assert out["loginId"] == "*****ER"
    assert out["tradingSymbol"] == "ONGC"
    assert mask_identifier("7") == "**"


def test_non_string_scalars_preserved() -> None:
    assert redact({"qty": 5, "flag": True, "none": None, "f": 1.5}) == {
        "qty": 5,
        "flag": True,
        "none": None,
        "f": 1.5,
    }


secret_chars = st.text(alphabet=string.ascii_letters + string.digits + "-_.", min_size=6)


@given(secret=secret_chars)
def test_query_token_never_survives(secret: str) -> None:
    out = redact_text(f"connect ?ACCESS_TOKEN={secret}&x=1")
    assert f"ACCESS_TOKEN={secret}" not in out


@given(
    st.recursive(st.text() | st.integers(), lambda c: st.lists(c) | st.dictionaries(st.text(), c))
)
def test_redaction_is_idempotent(value: object) -> None:
    once = redact(value)
    assert redact(once) == once
