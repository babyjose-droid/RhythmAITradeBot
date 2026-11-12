"""Users, login (password + TOTP), sessions, re-authentication. All outcomes audited."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.auth import passwords, totp
from rhythm.auth.secrets_box import SecretBox
from rhythm.config.settings import AuthSettings
from rhythm.core.errors import RhythmError
from rhythm.db.models import AuthSessionRow, UserRow

USERNAME = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")
ROLE_RANK = {"viewer": 0, "approver": 1, "owner": 2}


class AuthError(RhythmError):
    """Generic on purpose: never reveals whether the username, password or code was wrong."""


class UserExistsError(RhythmError):
    pass


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: str
    session_id: str | None
    reauth_at: datetime | None

    def at_least(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


@dataclass(frozen=True)
class NewSession:
    token: str
    csrf_token: str
    user: CurrentUser
    expires_at: datetime


@dataclass(frozen=True)
class Enrollment:
    username: str
    role: str
    totp_secret: str
    provisioning_uri: str


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _cu(u: UserRow, s: AuthSessionRow | None) -> CurrentUser:
    return CurrentUser(u.id, u.username, u.role, s.id if s else None, s.reauth_at if s else None)


class AuthService:
    def __init__(self, settings: AuthSettings, audit: AuditLog) -> None:
        key = settings.secret_key.get_secret_value() if settings.secret_key else ""
        self.settings = settings
        self.audit = audit
        self.box = SecretBox(key)

    # ------------------------------------------------------------------ users
    def has_users(self, session: Session) -> bool:
        return session.execute(select(UserRow.id).limit(1)).first() is not None

    def create_user(
        self,
        session: Session,
        *,
        username: str,
        password: str,
        role: str,
        created_by: str,
        at: datetime,
    ) -> Enrollment:
        username = username.strip().lower()
        if not USERNAME.match(username):
            raise ValueError("username: 3-64 chars, lowercase letters, digits, . _ -")
        if role not in ROLE_RANK:
            raise ValueError(f"role must be one of {sorted(ROLE_RANK)}")
        passwords.check_policy(password, username=username)
        if session.execute(select(UserRow.id).where(UserRow.username == username)).first():
            raise UserExistsError(f"user {username!r} already exists")
        secret = totp.new_secret()
        session.add(
            UserRow(
                username=username,
                role=role,
                password_hash=passwords.hash_password(password),
                totp_secret_sealed=self.box.seal(secret),
                created_by=created_by,
                password_changed_at=at,
            )
        )
        session.flush()
        self.audit.append(
            session,
            event_type="user.created",
            actor_type=ActorType.USER,
            actor_id=created_by,
            subject_type="user",
            subject_id=username,
            payload={"role": role},
        )
        return Enrollment(username, role, secret, totp.provisioning_uri(secret, username))

    def list_users(self, session: Session) -> list[UserRow]:
        return list(session.execute(select(UserRow).order_by(UserRow.id)).scalars())

    def set_disabled(
        self, session: Session, username: str, *, disabled: bool, actor: str, at: datetime
    ) -> UserRow:
        u = session.execute(
            select(UserRow).where(UserRow.username == username)
        ).scalar_one_or_none()
        if u is None:
            raise AuthError(f"no user {username!r}")
        if disabled and u.role == "owner":
            owners = [
                x for x in self.list_users(session) if x.role == "owner" and x.disabled_at is None
            ]
            if len(owners) <= 1:
                raise ValueError("cannot disable the last active owner")
        u.disabled_at = at if disabled else None
        if disabled:
            self._revoke_all(session, u.id, at)
        else:
            u.failed_logins, u.locked_until = 0, None
        self.audit.append(
            session,
            event_type="user.disabled" if disabled else "user.enabled",
            actor_type=ActorType.USER,
            actor_id=actor,
            subject_type="user",
            subject_id=username,
        )
        return u

    def reset_mfa(self, session: Session, username: str, *, actor: str, at: datetime) -> Enrollment:
        u = session.execute(
            select(UserRow).where(UserRow.username == username)
        ).scalar_one_or_none()
        if u is None:
            raise AuthError(f"no user {username!r}")
        secret = totp.new_secret()
        u.totp_secret_sealed, u.totp_last_step = self.box.seal(secret), None
        self._revoke_all(session, u.id, at)
        self.audit.append(
            session,
            event_type="user.mfa_reset",
            actor_type=ActorType.USER,
            actor_id=actor,
            subject_type="user",
            subject_id=username,
        )
        return Enrollment(u.username, u.role, secret, totp.provisioning_uri(secret, u.username))

    def change_password(
        self,
        session: Session,
        user: CurrentUser,
        *,
        current: str,
        new: str,
        code: str,
        at: datetime,
    ) -> None:
        u = session.get(UserRow, user.id, with_for_update=True)
        if u is None or not self._check(u, current, code, at):
            self._fail(session, u, user.username, at, "password_change")
            raise AuthError("invalid credentials")
        passwords.check_policy(new, username=u.username)
        u.password_hash, u.password_changed_at = passwords.hash_password(new), at
        self._revoke_all(session, u.id, at, keep=user.session_id)
        self.audit.append(
            session,
            event_type="auth.password_changed",
            actor_type=ActorType.USER,
            actor_id=u.username,
            subject_type="user",
            subject_id=u.username,
        )

    # ------------------------------------------------------------------ login
    def _check(self, u: UserRow, password: str, code: str, at: datetime) -> bool:
        ok_pw = passwords.verify_password(password, u.password_hash)
        if not self.settings.mfa_required:  # password-only mode (local only, see Settings)
            return ok_pw
        step = totp.verify(
            self.box.open(u.totp_secret_sealed), code, at, last_step=u.totp_last_step
        )
        if ok_pw and step is not None:
            u.totp_last_step = step  # a code is accepted once (replay protection)
            return True
        return False

    def _fail(
        self,
        session: Session,
        u: UserRow | None,
        username: str,
        at: datetime,
        what: str,
        ip: str = "",
    ) -> None:
        detail: dict[str, object] = {"what": what, "ip": ip}
        if u is not None:
            u.failed_logins += 1
            if u.failed_logins >= self.settings.max_failed_logins:
                u.locked_until = at + timedelta(minutes=self.settings.lockout_minutes)
                detail["locked_until"] = u.locked_until.isoformat()
        self.audit.append(
            session,
            event_type="auth.login_failed" if what == "login" else "auth.reauth_failed",
            actor_type=ActorType.USER,
            actor_id=username[:64] or "unknown",
            subject_type="user",
            subject_id=username[:64] or "unknown",
            payload=detail,
        )

    def login(
        self,
        session: Session,
        *,
        username: str,
        password: str,
        code: str,
        at: datetime,
        ip: str = "",
        user_agent: str = "",
    ) -> NewSession:
        name = username.strip().lower()[:64]
        u = session.execute(
            select(UserRow).where(UserRow.username == name).with_for_update()
        ).scalar_one_or_none()
        if u is None:
            passwords.verify_password(password, passwords.dummy_hash())
            self._fail(session, None, name, at, "login", ip)
            raise AuthError("invalid credentials")
        if u.disabled_at is not None or (u.locked_until is not None and u.locked_until > at):
            self._fail(session, None, name, at, "login", ip)  # no counting while locked
            raise AuthError("invalid credentials")
        if not self._check(u, password, code, at):
            self._fail(session, u, name, at, "login", ip)
            raise AuthError("invalid credentials")
        u.failed_logins, u.locked_until = 0, None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        absolute = at + timedelta(hours=self.settings.session_absolute_hours)
        row = AuthSessionRow(
            id=_sha(token),
            user_id=u.id,
            csrf_hash=_sha(csrf),
            created_at=at,
            last_seen_at=at,
            expires_at=absolute,
            reauth_at=at,  # a fresh login counts as a recent re-authentication
            ip=ip[:64],
            user_agent=user_agent[:200],
        )
        session.add(row)
        session.flush()
        self.audit.append(
            session,
            event_type="auth.login_succeeded",
            actor_type=ActorType.USER,
            actor_id=u.username,
            subject_type="user",
            subject_id=u.username,
            payload={"ip": ip[:64]},
        )
        return NewSession(token, csrf, _cu(u, row), absolute)

    def resolve(
        self,
        session: Session,
        token: str,
        at: datetime,
        *,
        csrf: str | None = None,
        check_csrf: bool = False,
    ) -> CurrentUser | None:
        """The user of a live session, or None. Mutating requests must pass the CSRF token."""
        if not token:
            return None
        s = session.get(AuthSessionRow, _sha(token))
        if s is None or s.revoked_at is not None or s.expires_at <= at:
            return None
        if at - s.last_seen_at > timedelta(minutes=self.settings.session_idle_minutes):
            s.revoked_at = at
            return None
        if check_csrf and not (csrf and hmac.compare_digest(_sha(csrf), s.csrf_hash)):
            raise AuthError("missing or invalid CSRF token")
        u = session.get(UserRow, s.user_id)
        if u is None or u.disabled_at is not None:
            return None
        if at - s.last_seen_at > timedelta(seconds=30):
            s.last_seen_at = at
        return _cu(u, s)

    def reauth(
        self, session: Session, user: CurrentUser, *, password: str, code: str, at: datetime
    ) -> datetime:
        u = session.get(UserRow, user.id, with_for_update=True)
        s = session.get(AuthSessionRow, user.session_id) if user.session_id else None
        if u is None or s is None or not self._check(u, password, code, at):
            self._fail(session, u, user.username, at, "reauth")
            raise AuthError("invalid credentials")
        s.reauth_at = at
        self.audit.append(
            session,
            event_type="auth.reauthenticated",
            actor_type=ActorType.USER,
            actor_id=u.username,
            subject_type="user",
            subject_id=u.username,
        )
        return at

    def recently_reauthenticated(self, user: CurrentUser, at: datetime) -> bool:
        return user.reauth_at is not None and at - user.reauth_at <= timedelta(
            minutes=self.settings.reauth_minutes
        )

    def logout(self, session: Session, user: CurrentUser, at: datetime) -> None:
        if user.session_id:
            s = session.get(AuthSessionRow, user.session_id)
            if s is not None and s.revoked_at is None:
                s.revoked_at = at
        self.audit.append(
            session,
            event_type="auth.logout",
            actor_type=ActorType.USER,
            actor_id=user.username,
            subject_type="user",
            subject_id=user.username,
        )

    def _revoke_all(
        self, session: Session, user_id: int, at: datetime, keep: str | None = None
    ) -> None:
        for s in session.execute(
            select(AuthSessionRow).where(
                AuthSessionRow.user_id == user_id, AuthSessionRow.revoked_at.is_(None)
            )
        ).scalars():
            if s.id != keep:
                s.revoked_at = at
