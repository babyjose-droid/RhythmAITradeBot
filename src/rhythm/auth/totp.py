"""Time-based one-time passwords (RFC 6238, SHA-1, 6 digits, 30 s), as used by
Google Authenticator, Authy, 1Password and similar apps."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import datetime
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1  # accept the previous and next step (clock drift)


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def step_of(at: datetime) -> int:
    return int(at.timestamp()) // STEP_SECONDS


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[off : off + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def verify(secret: str, code: str, at: datetime, *, last_step: int | None = None) -> int | None:
    """Return the matched step, or None. A step at or before ``last_step`` is a replay."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    now = step_of(at)
    for s in range(now - WINDOW, now + WINDOW + 1):
        if last_step is not None and s <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, s), code):
            return s
    return None


def provisioning_uri(secret: str, username: str, issuer: str = "Rhythm AI Trader") -> str:
    label = quote(f"{issuer}:{username}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )
