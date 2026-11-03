"""Research providers: a deterministic MOCK and the Anthropic Messages API.

Providers only turn a prompt into text. They never see a database, an order or a broker,
and they never raise anything but ``ProviderError`` to the caller.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_PACKET = re.compile(r"<evidence_packet>\s*(.*?)\s*</evidence_packet>", re.S)


class ProviderError(Exception):
    """kind: timeout | http | auth | malformed | unavailable."""

    def __init__(self, kind: str, message: str, attempts: int = 1) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts


@dataclass(frozen=True)
class ProviderRequest:
    system: str
    user: str
    max_output_tokens: int


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None
    attempts: int = 1
    tokens_estimated: bool = False


class ResearchProvider(Protocol):
    name: str
    model: str
    is_mock: bool

    def complete(self, req: ProviderRequest) -> ProviderResponse: ...


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ----------------------------------------------------------------------------- MOCK
class MockResearchProvider:
    """Deterministic stand-in labelled MOCK: reasons only from the packet it is given.

    ``scripted`` replays fixed responses (tests: malformed output, injected verdicts, ...).
    """

    name = "mock"
    model = "mock-research-1"
    is_mock = True

    def __init__(
        self, scripted: list[str] | None = None, fail: ProviderError | None = None
    ) -> None:
        self._scripted = list(scripted or [])
        self._fail = fail
        self.calls = 0

    def complete(self, req: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        if self._fail is not None:
            raise self._fail
        text = self._scripted.pop(0) if self._scripted else self._assess(req.user)
        return ProviderResponse(
            text=text,
            model=self.model,
            input_tokens=_estimate_tokens(req.system + req.user),
            output_tokens=_estimate_tokens(text),
            stop_reason="end_turn",
            tokens_estimated=True,
        )

    @staticmethod
    def _assess(user: str) -> str:
        m = _PACKET.search(user)
        if m is None:
            raise ProviderError("malformed", "mock provider: no evidence packet in prompt")
        p: dict[str, Any] = json.loads(m.group(1))
        facts = {f["id"]: f for f in p["facts"]}
        derived = {d["id"]: d for d in p["derived_metrics"]}
        fresh = p["freshness"]["quote_status"] in ("FRESH", "UNKNOWN")
        risk_ok = p["candidate"].get("risk_outcome") in (None, "ACCEPT", "REDUCE")
        gates = ("diag.entry_checks.", "diag.checks.", "diag.quality.")
        failed_checks = [
            k for k, f in facts.items() if k.startswith(gates) and f["value"] == "false"
        ]
        blocking = [x for x in p["missing"] if x.startswith("no published fundamentals")]
        observed = [
            {
                "statement": f"reference price {facts['price.ref']['value']} INR",
                "source_ids": ["price.ref"],
            }
        ]
        metrics = []
        for did in ("derived.roe", "derived.pe", "derived.debt_to_equity"):
            d = derived.get(did)
            if d and d["value"] is not None:
                metrics.append({"name": d["name"], "value": d["value"], "source_ids": [did]})
        if not fresh or blocking:
            action, sufficient = "REQUIRES_REVIEW", False
        elif not risk_ok:
            action, sufficient = "NO_ACTION", True
        elif failed_checks:
            action, sufficient = "HOLD", True
        else:
            action, sufficient = "CONFIRM", True
        docs = p["untrusted_documents"]
        out = {
            "symbol": p["symbol"],
            "horizon": p["horizon"],
            "as_of": p["as_of"],
            "freshness_status": p["freshness"]["quote_status"],
            "observed_facts": observed,
            "bull_case": [
                f"the rules produced the candidate: {'; '.join(p['candidate']['reasons'])}"[:400]
            ]
            if p["candidate"]["reasons"]
            else ["the rules produced the candidate"],
            "bear_case": ["MOCK assessment: no independent evidence beyond the packet"],
            "catalysts": [],
            "invalidation_conditions": ["the strategy's own exit or invalidation rules"],
            "key_metrics_with_provenance": metrics,
            "suggested_action": action,
            "rationale": "Deterministic MOCK assessment derived only from packet flags "
            "(freshness, risk outcome, strategy checks, missing evidence).",
            "confidence_explanation": "MOCK provider: not a model judgement.",
            "key_uncertainties": [*p["missing"][:4]]
            + (["untrusted documents were present and were not followed"] if docs else []),
            "change_view_conditions": ["licensed data or new evidence contradicting the rules"],
            "evidence_sufficient": sufficient,
        }
        return json.dumps(out)


# ----------------------------------------------------------------------------- Anthropic
class AnthropicProvider:
    """Calls the Anthropic Messages API with a timeout, bounded retries and backoff.

    The API key is held only in memory and sent only as the ``x-api-key`` header to the
    configured base URL; it is never logged or included in an error message.
    """

    name = "anthropic"
    is_mock = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not model:
            raise ValueError("AnthropicProvider needs an API key and a model id")
        self.model = model
        self._key = api_key
        self._max_retries = max_retries
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url, timeout=httpx.Timeout(timeout_seconds), transport=transport
        )

    def _backoff(self) -> Iterator[float]:
        delay = 1.0
        for _ in range(self._max_retries):
            yield delay
            delay = min(delay * 2, 8.0)

    def complete(self, req: ProviderRequest) -> ProviderResponse:
        body = {
            "model": self.model,
            "max_tokens": req.max_output_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
        }
        headers = {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        attempts = 0
        delays = self._backoff()
        while True:
            attempts += 1
            try:
                r = self._client.post("/v1/messages", json=body, headers=headers)
            except httpx.TimeoutException:
                err = ProviderError("timeout", "research provider timed out", attempts)
            except httpx.HTTPError as exc:
                err = ProviderError(
                    "unavailable", f"transport error: {type(exc).__name__}", attempts
                )
            else:
                if r.status_code == 200:
                    return self._parse(r, attempts)
                if r.status_code in (401, 403):
                    raise ProviderError(
                        "auth", f"provider rejected credentials ({r.status_code})", attempts
                    )
                err = ProviderError("http", f"provider returned HTTP {r.status_code}", attempts)
                if r.status_code not in RETRY_STATUS:
                    raise err
            delay = next(delays, None)
            if delay is None:
                raise err
            self._sleep(delay)

    def _parse(self, r: httpx.Response, attempts: int) -> ProviderResponse:
        try:
            data = r.json()
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            usage = data.get("usage", {})
            return ProviderResponse(
                text=text,
                model=str(data.get("model", self.model)),
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                stop_reason=data.get("stop_reason"),
                attempts=attempts,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(
                "malformed", f"unexpected provider response: {type(exc).__name__}", attempts
            ) from None
