"""用户名密码认证、数据库会话与登录失败保护。"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash
from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from services.auth.models import AuthSession, LoginAttempt, User

logger = logging.getLogger("auth.security")
password_hash = PasswordHash.recommended()
_dummy_password_hash = password_hash.hash(secrets.token_urlsafe(32))
_LOGIN_WINDOW = timedelta(minutes=15)
_LOGIN_LOCK = timedelta(minutes=15)
_LOGIN_USER_LIMIT = 5
_LOGIN_IP_LIMIT = 30


class LoginRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def validate_username(value: str) -> str:
    username = normalize_username(value)
    if not 3 <= len(username) <= 64:
        raise ValueError("USERNAME_LENGTH")
    if not all(char.isalnum() or char in "._-@" for char in username):
        raise ValueError("USERNAME_FORMAT")
    return username


def validate_password(value: str) -> None:
    if len(value) < 8:
        raise ValueError("PASSWORD_TOO_SHORT")
    if len(value) > 128:
        raise ValueError("PASSWORD_TOO_LONG")


def session_ttl() -> int:
    return min(30 * 24 * 3600, max(300, int(os.getenv("AUTH_SESSION_TTL_SECONDS", str(12 * 3600)))))


def _rate_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rate_keys(username: str, client_ip: str) -> tuple[str, str]:
    return _rate_key(normalize_username(username) or "<empty>"), _rate_key(client_ip or "unknown")


async def _blocked_for(db: AsyncSession, username_key: str, ip_key: str) -> int:
    now = datetime.now(timezone.utc)
    rows = await db.scalars(
        select(LoginAttempt.locked_until).where(
            ((LoginAttempt.scope == "username") & (LoginAttempt.key_hash == username_key))
            | ((LoginAttempt.scope == "ip") & (LoginAttempt.key_hash == ip_key))
        )
    )
    deadlines = [deadline for deadline in rows.all() if deadline and deadline > now]
    return max((int((deadline - now).total_seconds()) for deadline in deadlines), default=0)


async def _record_failed_attempt(
    db: AsyncSession, scope: str, key_hash: str, threshold: int, now: datetime
) -> None:
    reset_before = now - _LOGIN_WINDOW
    statement = insert(LoginAttempt).values(
        scope=scope,
        key_hash=key_hash,
        failed_count=1,
        window_started_at=now,
        locked_until=None,
        updated_at=now,
    )
    reset_window = LoginAttempt.window_started_at < reset_before
    statement = statement.on_conflict_do_update(
        index_elements=[LoginAttempt.scope, LoginAttempt.key_hash],
        set_={
            "failed_count": case(
                (reset_window, 1), else_=LoginAttempt.failed_count + 1
            ),
            "window_started_at": case(
                (reset_window, now), else_=LoginAttempt.window_started_at
            ),
            "locked_until": case(
                (reset_window, None),
                (LoginAttempt.failed_count + 1 >= threshold, now + _LOGIN_LOCK),
                else_=LoginAttempt.locked_until,
            ),
            "updated_at": now,
        },
    )
    await db.execute(statement)


async def register_user(db: AsyncSession, username: str, password: str) -> User:
    normalized = validate_username(username)
    validate_password(password)
    existing = await db.scalar(select(User.id).where(User.username == normalized))
    if existing:
        raise ValueError("USERNAME_TAKEN")
    user = User(
        id=str(uuid.uuid4()),
        username=normalized,
        password_hash=password_hash.hash(password),
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def login_user(
    db: AsyncSession, username: str, password: str, client_ip: str
) -> tuple[User, str, datetime] | None:
    username_key, ip_key = _rate_keys(username, client_ip)
    retry_after = await _blocked_for(db, username_key, ip_key)
    if retry_after:
        logger.warning(
            "login_rate_limited ip_key=%s username_key=%s", ip_key[:16], username_key[:16]
        )
        raise LoginRateLimited(retry_after)

    normalized = normalize_username(username)
    user = await db.scalar(
        select(User).where(User.username == normalized, User.status == "active")
    )
    candidate_hash = user.password_hash if user else _dummy_password_hash
    password_valid = password_hash.verify(password, candidate_hash)
    if not user or not password_valid:
        now = datetime.now(timezone.utc)
        await _record_failed_attempt(db, "username", username_key, _LOGIN_USER_LIMIT, now)
        await _record_failed_attempt(db, "ip", ip_key, _LOGIN_IP_LIMIT, now)
        # Keep only the current 24-hour window. This log deliberately excludes credentials,
        # raw usernames, raw IP addresses, tokens, and request bodies.
        await db.execute(
            delete(LoginAttempt).where(LoginAttempt.updated_at < now - timedelta(days=1))
        )
        await db.commit()
        logger.warning("login_failed reason=invalid_credentials ip_key=%s", ip_key[:16])
        return None

    await db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.scope == "username", LoginAttempt.key_hash == username_key
        )
    )
    raw_token, expires_at = await create_auth_session(db, user)
    logger.info("login_succeeded user_id=%s", user.id)
    return user, raw_token, expires_at


async def create_auth_session(db: AsyncSession, user: User) -> tuple[str, datetime]:
    raw_token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=session_ttl())
    db.add(
        AuthSession(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=expires_at,
        )
    )
    await db.commit()
    return raw_token, expires_at


async def get_user_by_token(db: AsyncSession, raw_token: str | None) -> User | None:
    if not raw_token:
        return None
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(
            AuthSession.token_hash == digest,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
            User.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def revoke_token(db: AsyncSession, raw_token: str | None) -> None:
    if not raw_token:
        return
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    await db.execute(
        update(AuthSession)
        .where(AuthSession.token_hash == digest, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()
