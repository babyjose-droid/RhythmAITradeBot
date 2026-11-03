"""Encryption at rest for TOTP secrets (Fernet: AES-128-CBC + HMAC-SHA256)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str) -> None:
        if len(key) < 32:
            raise ValueError("secret key must have at least 32 characters")
        derived = hashlib.sha256(b"rhythm-totp-v1:" + key.encode()).digest()
        self._f = Fernet(base64.urlsafe_b64encode(derived))

    def seal(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def open(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("cannot decrypt: wrong RHYTHM_AUTH__SECRET_KEY?") from exc
