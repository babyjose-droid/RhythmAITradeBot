"""Request authentication and authorisation (Phase 8).

Every route depends on ``access``. It resolves the session cookie, checks the CSRF token on
mutating requests and applies the permission table below. The signed-in user becomes the
actor of every audit event (``actor``). With auth disabled (local/test only) every request
acts as ``settings.local_actor_id`` with the owner role, as in earlier phases.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from contextvars import ContextVar
from typing import Any

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from rhythm.api.state import AppState
from rhythm.auth.service import AuthError, CurrentUser
from rhythm.db.session import session_scope

SESSION_COOKIE = "rhythm_session"
CSRF_COOKIE = "rhythm_csrf"
CSRF_HEADER = "x-csrf-token"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

PUBLIC = frozenset(
    {
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/v1/auth/status"),
        ("POST", "/v1/auth/login"),
    }
)
# Any signed-in user, any role (their own session and inbox).
SELF = frozenset(
    {
        ("POST", "/v1/auth/logout"),
        ("POST", "/v1/auth/reauth"),
        ("POST", "/v1/auth/password"),
        ("POST", "/v1/notifications/read"),
    }
)
# Approver or owner. (Releasing the kill switch additionally needs owner + re-auth.)
APPROVER = frozenset(
    {
        ("POST", "/v1/approvals/{approval_id}/decision"),
        ("POST", "/v1/risk/kill-switch"),
    }
)
# Owner with a re-authentication in the last few minutes.
SENSITIVE = frozenset(
    {
        ("POST", "/v1/risk/policies"),
        ("POST", "/v1/strategies/{name}/resume"),
        ("PUT", "/v1/paper/accounts/{account_id}/ledgers/{horizon}"),
        ("POST", "/v1/users"),
        ("POST", "/v1/users/{username}/disable"),
        ("POST", "/v1/users/{username}/enable"),
        ("POST", "/v1/users/{username}/reset-mfa"),
    }
)
OWNER_READ = frozenset({("GET", "/v1/users")})

_user: ContextVar[CurrentUser | None] = ContextVar("rhythm_user", default=None)


def current_user() -> CurrentUser | None:
    return _user.get()


def actor(state: AppState) -> str:
    u = _user.get()
    return u.username if u is not None else state.settings.local_actor_id


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", request.url.path))


def require_recent_auth(state: AppState, *, role: str = "owner") -> CurrentUser:
    u = _user.get()
    if u is None:
        raise HTTPException(401, "authentication required")
    if not u.at_least(role):
        raise HTTPException(403, f"forbidden: requires the {role} role")
    if state.auth is not None and not state.auth.recently_reauthenticated(u, state.clock.now()):
        raise HTTPException(403, "reauth_required")
    return u


class LoginRateLimiter:
    """At most ``limit`` login attempts per client IP per ``window`` seconds (in memory)."""

    def __init__(self, limit: int = 20, window: float = 900.0) -> None:
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        q = self._hits[ip]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


async def access(request: Request) -> None:
    state: AppState = request.app.state.rhythm
    method, path = request.method.upper(), _route_path(request)
    if state.auth is None:  # auth disabled (local/test only; refused elsewhere by Settings)
        now = state.clock.now()
        _user.set(CurrentUser(0, state.settings.local_actor_id, "owner", None, now))
        return
    if (method, path) in PUBLIC:
        _user.set(None)
        return
    token = request.cookies.get(SESSION_COOKIE, "")
    csrf = request.headers.get(CSRF_HEADER)
    auth = state.auth

    def resolve() -> CurrentUser | None:
        with session_scope(state.session_factory) as s:
            return auth.resolve(
                s, token, state.clock.now(), csrf=csrf, check_csrf=method in MUTATING
            )

    try:
        user = await run_in_threadpool(resolve)
    except AuthError as exc:
        raise HTTPException(403, str(exc)) from None
    if user is None:
        raise HTTPException(401, "authentication required")
    _user.set(user)
    if method not in MUTATING:
        if (method, path) in OWNER_READ and not user.at_least("owner"):
            raise HTTPException(403, "forbidden: requires the owner role")
        return
    key = (method, path)
    if key in SELF:
        return
    if key in APPROVER:
        if not user.at_least("approver"):
            raise HTTPException(403, "forbidden: requires the approver or owner role")
        return
    if not user.at_least("owner"):
        raise HTTPException(403, "forbidden: requires the owner role")
    if key in SENSITIVE and not auth.recently_reauthenticated(user, state.clock.now()):
        raise HTTPException(403, "reauth_required")


def cookie_kwargs(state: AppState, max_age: int) -> dict[str, Any]:
    return {
        "max_age": max_age,
        "path": "/",
        "secure": state.settings.cookie_secure,
        "samesite": "strict",
    }
