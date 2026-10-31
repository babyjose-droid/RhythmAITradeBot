"""Synthetic test values. Built at runtime so secret scanners don't flag source literals.

These are NOT credentials: the JWT decodes to {"sub": "fake-user"} with a fake signature.
"""

from __future__ import annotations

import base64
import json


def _b64(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


FAKE_JWT = ".".join([_b64({"alg": "HS256"}), _b64({"sub": "fake-user"}), _b64("fakesignature")])
