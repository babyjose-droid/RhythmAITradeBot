"""Redaction of secrets and identifiers before anything is logged or audited.

Conservative by design: a false positive (over-redaction) is acceptable, a leak is not.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "***REDACTED***"

_SENSITIVE_KEY = re.compile(
    r"(token|secret|password|passwd|pwd|api[_-]?key|vendor[_-]?key|authorization|cookie|"
    r"credential|private[_-]?key|session|signature|otp|totp|pin)",
    re.IGNORECASE,
)
_IDENTIFIER_KEY = re.compile(
    r"^(customer[_-]?id|login[_-]?id|channel[_-]?user|client[_-]?id)$", re.I
)

_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JSON Web Tokens
    (re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+"), REDACTED),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    # Sensitive query-string / form parameters (e.g. Sharekhan WebSocket ?ACCESS_TOKEN=...)
    (
        re.compile(
            r"(?i)\b(access[_-]?token|api[_-]?key|request[_-]?token|vendor[_-]?key|secret[_-]?key|"
            r"password|state)=([^&\s\"']+)"
        ),
        r"\1=" + REDACTED,
    ),
    # Credentials embedded in URLs: scheme://user:password@host
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]+):([^@\s]+)@"), r"\1:" + REDACTED + "@"),
)


def mask_identifier(value: object) -> str:
    """Keep only the last two characters of an account identifier."""
    text = str(value)
    if len(text) <= 2:
        return "**"
    return "*" * (len(text) - 2) + text[-2:]


def redact_text(text: str) -> str:
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a redacted deep copy of ``value`` (dicts, lists, tuples, strings)."""
    if _depth > 32:
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if is_sensitive_key(key_str):
                out[key] = REDACTED
            elif _IDENTIFIER_KEY.match(key_str) and item is not None:
                out[key] = mask_identifier(item)
            else:
                out[key] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, _depth=_depth + 1) for v in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
