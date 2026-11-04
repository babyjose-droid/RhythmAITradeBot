"""Sharekhan request-token transform (documented protocol; see SHAREKHAN_INTEGRATION.md §3).

The request token returned to the redirect URL decrypts (AES-256-GCM, key = the customer's
32-byte Secure Key, 16-byte all-zero nonce, 128-bit tag appended) to ``RequestId|CustomerId``.
It is swapped to ``CustomerId|RequestId`` and re-encrypted the same way before the access-token
call. ``version_id=1005`` uses URL-safe Base64, otherwise standard Base64.

The all-zero nonce is the broker's protocol, not a choice made here (SECURITY_MODEL.md).
UNVERIFIED against a live response until the supervised read-only session.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from rhythm.broker.base import BrokerAuthError

NONCE = bytes(16)


@dataclass(frozen=True)
class RequestToken:
    request_id: str
    customer_id: str


def _b64decode(s: str, urlsafe: bool) -> bytes:
    s = s.strip()
    padded = s + "=" * (-len(s) % 4)
    try:
        return (
            base64.urlsafe_b64decode(padded) if urlsafe else base64.b64decode(padded, validate=True)
        )
    except (binascii.Error, ValueError) as exc:
        raise BrokerAuthError("request token is not valid Base64") from exc


def _b64encode(b: bytes, urlsafe: bool) -> str:
    return (base64.urlsafe_b64encode(b) if urlsafe else base64.b64encode(b)).decode()


def _cipher(secret_key: str) -> AESGCM:
    key = secret_key.encode()
    if len(key) != 32:
        raise BrokerAuthError("the Sharekhan Secure Key must be 32 bytes")
    return AESGCM(key)


def decrypt(token: str, secret_key: str, *, urlsafe: bool = True) -> RequestToken:
    try:
        plain = _cipher(secret_key).decrypt(NONCE, _b64decode(token, urlsafe), None).decode()
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise BrokerAuthError("request token did not decrypt with this Secure Key") from exc
    parts = plain.split("|")
    if len(parts) != 2 or not all(parts):
        raise BrokerAuthError("decrypted request token is not 'RequestId|CustomerId'")
    return RequestToken(request_id=parts[0], customer_id=parts[1])


def encrypt(plain: str, secret_key: str, *, urlsafe: bool = True) -> str:
    return _b64encode(_cipher(secret_key).encrypt(NONCE, plain.encode(), None), urlsafe)


def manipulate(token: str, secret_key: str, *, urlsafe: bool = True) -> tuple[str, str]:
    """Return (manipulated token for the access-token call, customer id)."""
    rt = decrypt(token, secret_key, urlsafe=urlsafe)
    return encrypt(f"{rt.customer_id}|{rt.request_id}", secret_key, urlsafe=urlsafe), rt.customer_id
