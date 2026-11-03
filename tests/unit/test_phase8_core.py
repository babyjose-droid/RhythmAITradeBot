"""Phase 8 core: TOTP (RFC 6238 vectors), password hashing, secret box, rate limiter,
approval override in the risk engine, approval expiry."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from rhythm.api.auth import LoginRateLimiter
from rhythm.auth import passwords, totp
from rhythm.auth.secrets_box import SecretBox
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Horizon
from rhythm.paper.approvals import expiry
from rhythm.risk.engine import RiskOutcome, evaluate
from rhythm.risk.policy import DEFAULT_POLICY
from tests.unit.test_risk_engine import prop, state

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(
    ("t", "code"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_totp_rfc6238_vectors(t: int, code: str) -> None:
    at = datetime.fromtimestamp(t, UTC)
    assert totp.code_at(RFC_SECRET, totp.step_of(at)) == code
    assert totp.verify(RFC_SECRET, code, at) == totp.step_of(at)


def test_totp_window_replay_and_format() -> None:
    at = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    s = totp.new_secret()
    step = totp.step_of(at)
    prev = totp.code_at(s, step - 1)
    assert totp.verify(s, prev, at) == step - 1  # one step of drift allowed
    assert totp.verify(s, totp.code_at(s, step - 3), at) is None
    assert totp.verify(s, totp.code_at(s, step), at, last_step=step) is None  # replay
    assert totp.verify(s, "12345", at) is None and totp.verify(s, "abcdef", at) is None
    uri = totp.provisioning_uri(s, "owner")
    assert (
        uri.startswith("otpauth://totp/Rhythm%20AI%20Trader%3Aowner?secret=") and "period=30" in uri
    )


def test_password_hash_and_policy() -> None:
    h = passwords.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert passwords.verify_password("correct horse battery", h)
    assert not passwords.verify_password("correct horse batterY", h)
    assert not passwords.verify_password("x", "garbage")
    assert passwords.hash_password("correct horse battery") != h  # salted
    for bad, msg in (
        ("short", "at least"),
        ("aaaaaaaaaaaaaa", "repetitive"),
        ("owner-is-me-123", "username"),
    ):
        with pytest.raises(passwords.WeakPasswordError, match=msg):
            passwords.check_policy(bad, username="owner")


def test_secret_box_roundtrip_and_wrong_key() -> None:
    box = SecretBox("k" * 40)
    sealed = box.seal("JBSWY3DPEHPK3PXP")
    assert "JBSWY3DPEHPK3PXP" not in sealed and box.open(sealed) == "JBSWY3DPEHPK3PXP"
    with pytest.raises(ValueError, match="decrypt"):
        SecretBox("z" * 40).open(sealed)
    with pytest.raises(ValueError):
        SecretBox("short")


def test_login_rate_limiter() -> None:
    lim = LoginRateLimiter(limit=3, window=60)
    assert [lim.allow("1.2.3.4") for _ in range(4)] == [True, True, True, False]
    assert lim.allow("5.6.7.8")


def test_approval_satisfies_only_the_review_policy() -> None:
    pol = replace(DEFAULT_POLICY, version="t", review_above_order_value=Decimal(10_000))
    p = prop()
    st = state()
    assert evaluate(p, st, pol).outcome is RiskOutcome.REVIEW
    approved = evaluate(replace(p, approved_by="owner"), st, pol)
    assert approved.outcome in (RiskOutcome.ACCEPT, RiskOutcome.REDUCE) and approved.accepted
    blocked = evaluate(replace(p, approved_by="owner"), replace(st, kill_switch_active=True), pol)
    assert blocked.outcome is RiskOutcome.BLOCK  # an approval never overrides a failed check


def test_approval_expiry_by_horizon() -> None:
    cal = SessionCalendar()
    at = datetime(2026, 10, 30, 11, 0, tzinfo=EXCHANGE_TZ)  # Friday
    assert expiry(cal, Horizon.INTRADAY, at) == cal.session_close(date(2026, 10, 30))
    assert expiry(cal, Horizon.SWING, at) == cal.session_close(date(2026, 11, 2))  # Monday
    late = datetime(2026, 10, 30, 16, 0, tzinfo=EXCHANGE_TZ)
    assert expiry(cal, Horizon.INTRADAY, late) == late + timedelta(minutes=5)
