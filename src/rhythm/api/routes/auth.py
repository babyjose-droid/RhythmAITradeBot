"""Sign-in (password + authenticator code), sessions, re-authentication, users (Phase 8)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from rhythm.api.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    LoginRateLimiter,
    actor,
    client_ip,
    cookie_kwargs,
    current_user,
)
from rhythm.api.deps import SessionDep, StateDep
from rhythm.auth.passwords import WeakPasswordError
from rhythm.auth.service import AuthError, AuthService, Enrollment, UserExistsError

router = APIRouter(tags=["auth"])
_limiter = LoginRateLimiter()
SETUP_HINT = (
    "No users yet. Create the owner on the server: "
    "docker compose exec api rhythm-cli user-create --username owner --role owner"
)


def _auth(state: StateDep) -> AuthService:
    if state.auth is None:
        raise HTTPException(404, "authentication is disabled in this environment (local demo)")
    return state.auth


def _denied(msg: str = "invalid credentials", code: int = 401) -> JSONResponse:
    # Returned (not raised) so failed-attempt counters and audit rows are committed.
    return JSONResponse({"detail": msg}, status_code=code)


@router.get("/v1/auth/status")
def status(state: StateDep, session: SessionDep) -> dict[str, Any]:
    if state.auth is None:
        return {"enabled": False, "has_users": False, "note": "auth disabled (local/test only)"}
    has = state.auth.has_users(session)
    return {
        "enabled": True,
        "mfa_required": state.auth.settings.mfa_required,
        "has_users": has,
        "setup_hint": None if has else SETUP_HINT,
    }


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(default="", max_length=8)  # empty when mfa_required=false


@router.post("/v1/auth/login")
def login(body: LoginIn, request: Request, state: StateDep, session: SessionDep) -> Any:
    auth = _auth(state)
    ip = client_ip(request)
    if not _limiter.allow(ip):
        return _denied("too many attempts; try again later", 429)
    try:
        ns = auth.login(
            session,
            username=body.username,
            password=body.password,
            code=body.code,
            at=state.clock.now(),
            ip=ip,
            user_agent=request.headers.get("user-agent", ""),
        )
    except AuthError:
        return _denied()
    max_age = int((ns.expires_at - state.clock.now()).total_seconds())
    resp = JSONResponse(
        {
            "user": {"username": ns.user.username, "role": ns.user.role},
            "csrf_token": ns.csrf_token,
            "expires_at": ns.expires_at.isoformat(),
        }
    )
    resp.set_cookie(SESSION_COOKIE, ns.token, httponly=True, **cookie_kwargs(state, max_age))
    resp.set_cookie(CSRF_COOKIE, ns.csrf_token, httponly=False, **cookie_kwargs(state, max_age))
    return resp


@router.post("/v1/auth/logout")
def logout(state: StateDep, session: SessionDep, response: Response) -> dict[str, Any]:
    u = current_user()
    if state.auth is not None and u is not None:
        state.auth.logout(session, u, state.clock.now())
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"logged_out": True}


@router.get("/v1/auth/me")
def me(state: StateDep) -> dict[str, Any]:
    u = current_user()
    if u is None:
        raise HTTPException(401, "authentication required")
    recent = state.auth is None or state.auth.recently_reauthenticated(u, state.clock.now())
    return {
        "username": u.username,
        "role": u.role,
        "auth_enabled": state.auth is not None,
        "mfa_required": state.auth.settings.mfa_required if state.auth else False,
        "recently_reauthenticated": recent,
    }


class ReauthIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(default="", max_length=8)  # empty when mfa_required=false


@router.post("/v1/auth/reauth")
def reauth(body: ReauthIn, state: StateDep, session: SessionDep) -> Any:
    auth, u = _auth(state), current_user()
    if u is None:
        raise HTTPException(401, "authentication required")
    try:
        at = auth.reauth(session, u, password=body.password, code=body.code, at=state.clock.now())
    except AuthError:
        return _denied()
    return {"reauthenticated_at": at.isoformat(), "valid_minutes": auth.settings.reauth_minutes}


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)
    code: str = Field(default="", max_length=8)  # empty when mfa_required=false


@router.post("/v1/auth/password")
def change_password(body: PasswordIn, state: StateDep, session: SessionDep) -> Any:
    auth, u = _auth(state), current_user()
    if u is None:
        raise HTTPException(401, "authentication required")
    try:
        auth.change_password(
            session,
            u,
            current=body.current_password,
            new=body.new_password,
            code=body.code,
            at=state.clock.now(),
        )
    except AuthError:
        return _denied()
    except WeakPasswordError as exc:
        return _denied(str(exc), 422)
    return {"changed": True, "other_sessions_revoked": True}


# ---------------------------------------------------------------------------- users (owner)
def _enrollment(e: Enrollment) -> dict[str, Any]:
    return {
        "username": e.username,
        "role": e.role,
        "totp_secret": e.totp_secret,
        "provisioning_uri": e.provisioning_uri,
        "note": "Shown once. Add it to an authenticator app now; it cannot be displayed again.",
    }


@router.get("/v1/users")
def users(state: StateDep, session: SessionDep) -> list[dict[str, Any]]:
    return [
        {
            "username": u.username,
            "role": u.role,
            "created_at": u.created_at,
            "created_by": u.created_by,
            "disabled": u.disabled_at is not None,
            "locked_until": u.locked_until,
            "failed_logins": u.failed_logins,
        }
        for u in _auth(state).list_users(session)
    ]


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    role: Literal["owner", "approver", "viewer"]
    password: str = Field(min_length=1, max_length=256)


@router.post("/v1/users", status_code=201)
def create_user(body: UserIn, state: StateDep, session: SessionDep) -> Any:
    try:
        e = _auth(state).create_user(
            session,
            username=body.username,
            password=body.password,
            role=body.role,
            created_by=actor(state),
            at=state.clock.now(),
        )
    except UserExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _enrollment(e)


def _set_disabled(
    username: str, disabled: bool, state: StateDep, session: SessionDep
) -> dict[str, Any]:
    try:
        u = _auth(state).set_disabled(
            session, username, disabled=disabled, actor=actor(state), at=state.clock.now()
        )
    except AuthError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"username": u.username, "disabled": u.disabled_at is not None}


@router.post("/v1/users/{username}/disable")
def disable_user(username: str, state: StateDep, session: SessionDep) -> dict[str, Any]:
    return _set_disabled(username, True, state, session)


@router.post("/v1/users/{username}/enable")
def enable_user(username: str, state: StateDep, session: SessionDep) -> dict[str, Any]:
    return _set_disabled(username, False, state, session)


@router.post("/v1/users/{username}/reset-mfa")
def reset_mfa(username: str, state: StateDep, session: SessionDep) -> dict[str, Any]:
    try:
        e = _auth(state).reset_mfa(session, username, actor=actor(state), at=state.clock.now())
    except AuthError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _enrollment(e)
