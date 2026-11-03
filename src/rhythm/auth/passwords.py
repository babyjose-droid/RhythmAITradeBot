"""Password hashing with scrypt (stdlib). Format: scrypt$n$r$p$salt_b64$hash_b64."""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets

N, R, P, DKLEN = 2**15, 8, 1, 32
MIN_LENGTH = 12
MAX_LENGTH = 256
_MAXMEM = 64 * 1024 * 1024


class WeakPasswordError(ValueError):
    pass


def check_policy(password: str, *, username: str = "") -> None:
    if len(password) < MIN_LENGTH:
        raise WeakPasswordError(f"password must have at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise WeakPasswordError("password is too long")
    if username and username.lower() in password.lower():
        raise WeakPasswordError("password must not contain the username")
    if len(set(password)) < 6:
        raise WeakPasswordError("password is too repetitive")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN, maxmem=_MAXMEM)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, expected = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(expected)),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(expected))


@functools.cache
def dummy_hash() -> str:
    """Checked when the username does not exist, so a miss costs as much as a wrong password."""
    return hash_password(secrets.token_urlsafe(16))
