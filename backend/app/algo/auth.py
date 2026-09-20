"""Server-side admin auth for the Algo Config tab.

This is REAL authentication (§11.4), unlike the display-only dashboard gates:
credentials are verified against a hashed row in ``algo_users`` and every
``/api/algo/*`` endpoint (except login itself) requires a signed session token
carried in an httpOnly cookie. The existing "/" dashboard gate is untouched.

Design constraints honoured:
- Zero new dependencies: PBKDF2-HMAC-SHA256 (stdlib ``hashlib``) for password
  hashing, stdlib ``hmac`` for the session signature.
- One admin for now, BOOTSTRAPPED from ALGO_ADMIN_USER / ALGO_ADMIN_PASSWORD
  when the users table is empty; the ``role`` column already supports
  admin|editor|viewer for the later RBAC build (§11.2).
- The DATABASE is the source of truth, not the environment (2026-09-13).
  ``algo_users`` holds the live credentials and ``change_credentials`` below
  rewrites them in place, so an ID or password changed in the browser survives
  a restart. The env vars only ever seed an EMPTY table, so they can be removed
  from ``.env`` once the first admin exists. Access is gated on "an admin row
  exists", never on the env vars still being present — gating on the env is what
  used to force the secret to stay on disk forever.
- Session secret comes from ALGO_SESSION_SECRET; when blank (dev default) an
  ephemeral per-boot secret is generated — sessions then die on restart, which
  is safe-by-default rather than shared-secret-by-default.
- Token shape: ``user_id.expires_epoch.hex(hmac_sha256(secret, payload))`` —
  self-contained, constant-time verified, no server-side session table.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import structlog
from fastapi import HTTPException, Request
from sqlalchemy import text

from ..core.config import settings
from ..core.db import AsyncSessionLocal

log = structlog.get_logger(__name__)

SESSION_COOKIE = "algo_session"

_PBKDF2_ITERATIONS = 200_000
_PBKDF2_ALGO = "sha256"

# Ephemeral fallback secret (per boot) when ALGO_SESSION_SECRET is unset.
_ephemeral_secret = secrets.token_hex(32)

_SELECT_USER_SQL = text(
    "SELECT user_id, username, password_hash, role FROM algo_users WHERE username = :username"
)
_INSERT_USER_SQL = text(
    """
    INSERT INTO algo_users (username, password_hash, role)
    VALUES (:username, :password_hash, 'admin')
    ON CONFLICT (username) DO NOTHING
    """
)
_COUNT_USERS_SQL = text("SELECT COUNT(*) AS n FROM algo_users")
_UPDATE_CREDENTIALS_SQL = text(
    """
    UPDATE algo_users
    SET username = :new_username, password_hash = :new_password_hash
    WHERE user_id = :user_id
    """
)
_USERNAME_TAKEN_SQL = text(
    "SELECT 1 FROM algo_users WHERE lower(username) = lower(:username) AND user_id <> :user_id"
)


@dataclass(frozen=True)
class AdminIdentity:
    user_id: int
    username: str
    role: str


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` — self-describing so
    iteration bumps later can coexist with old rows."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO, password.encode(), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGO, password.encode(), bytes.fromhex(salt_hex), int(iterations_s)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def _secret() -> bytes:
    configured = settings.algo_session_secret
    return (configured or _ephemeral_secret).encode()


def issue_token(user_id: int, *, now: Optional[float] = None) -> str:
    expires = int((now if now is not None else time.time()) + settings.algo_session_ttl_h * 3600)
    payload = f"{user_id}.{expires}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str, *, now: Optional[float] = None) -> Optional[int]:
    """→ user_id when the token is authentic and unexpired, else None."""
    try:
        user_id_s, expires_s, sig = token.split(".")
        payload = f"{user_id_s}.{expires_s}"
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if (now if now is not None else time.time()) >= int(expires_s):
            return None
        return int(user_id_s)
    except (ValueError, AttributeError):
        return None


async def ensure_admin_seeded() -> None:
    """Insert the admin row from ALGO_ADMIN_USER / ALGO_ADMIN_PASSWORD when the
    users table is empty. Called lazily from the login path so a fresh database
    works without a separate provisioning step. Idempotent."""
    if not settings.algo_admin_user or not settings.algo_admin_password:
        return
    async with AsyncSessionLocal() as session:
        n = (await session.execute(_COUNT_USERS_SQL)).scalar_one()
        if n and int(n) > 0:
            return
        await session.execute(
            _INSERT_USER_SQL,
            {
                "username": settings.algo_admin_user,
                "password_hash": hash_password(settings.algo_admin_password),
            },
        )
        await session.commit()
        log.info("algo.auth.admin_seeded", username=settings.algo_admin_user)


async def user_count() -> int:
    """How many admin rows exist. 0 = nothing has been bootstrapped yet."""
    async with AsyncSessionLocal() as session:
        n = (await session.execute(_COUNT_USERS_SQL)).scalar_one()
    return int(n or 0)


async def auth_configured() -> bool:
    """Is there any way to sign in?

    True when an admin row already exists (the database is the source of truth)
    OR when the env vars can still bootstrap one. Deliberately NOT "the env vars
    are set": that older test locked the credentials to ``.env`` forever, so a
    password changed in the browser could not be accompanied by deleting the
    file it came from. A misconfigured stack with neither still fails loud.
    """
    if await user_count() > 0:
        return True
    return bool(settings.algo_admin_user and settings.algo_admin_password)


class CredentialError(ValueError):
    """Why a credential change was refused. Safe to show the user."""


async def change_credentials(
    *,
    user_id: int,
    current_username: str,
    current_password: str,
    new_username: str,
    new_password: str,
) -> AdminIdentity:
    """Rewrite one admin's username and password after re-proving the old pair.

    Re-authentication is done against the SIGNED-IN user's own row, so a valid
    session for user A can never be used to rewrite user B: the row is selected
    by ``user_id`` from the session, and the supplied username must match it.
    Raises ``CredentialError`` with a user-safe message on every refusal.
    """
    new_username = (new_username or "").strip()
    if not new_username:
        raise CredentialError("The new ID cannot be blank.")
    if len(new_username) > 64:
        raise CredentialError("The new ID is too long (64 characters maximum).")
    if not new_password or len(new_password) < 8:
        raise CredentialError("The new password must be at least 8 characters.")

    ident = await authenticate(current_username, current_password)
    if ident is None or ident.user_id != user_id:
        # Wrong old pair, or the right pair for a DIFFERENT account than the
        # one this session holds. Both are refusals, worded identically so the
        # response cannot be used to probe which usernames exist.
        raise CredentialError("The current ID or password is not correct.")

    async with AsyncSessionLocal() as session:
        taken = (
            await session.execute(
                _USERNAME_TAKEN_SQL, {"username": new_username, "user_id": user_id}
            )
        ).first()
        if taken is not None:
            raise CredentialError("That ID is already in use.")
        await session.execute(
            _UPDATE_CREDENTIALS_SQL,
            {
                "user_id": user_id,
                "new_username": new_username,
                "new_password_hash": hash_password(new_password),
            },
        )
        await session.commit()
    log.info("algo.auth.credentials_changed", user_id=user_id, username=new_username)
    return AdminIdentity(user_id=user_id, username=new_username, role=ident.role)


async def authenticate(username: str, password: str) -> Optional[AdminIdentity]:
    """Verify credentials against algo_users. None on any failure — the caller
    audits the attempt either way."""
    await ensure_admin_seeded()
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(_SELECT_USER_SQL, {"username": username})
        ).mappings().first()
    if row is None:
        # Burn comparable time so a missing username is not distinguishable
        # from a wrong password by response latency.
        verify_password(password, hash_password("timing-equalizer"))
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return AdminIdentity(
        user_id=int(row["user_id"]), username=row["username"], role=row["role"]
    )


async def _identity_from_user_id(user_id: int) -> Optional[AdminIdentity]:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT user_id, username, role FROM algo_users WHERE user_id = :uid"),
                {"uid": user_id},
            )
        ).mappings().first()
    if row is None:
        return None
    return AdminIdentity(
        user_id=int(row["user_id"]), username=row["username"], role=row["role"]
    )


_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}


def role_rank(role: str) -> int:
    return _ROLE_RANK.get(role, 0)


async def require_admin(request: Request) -> AdminIdentity:
    """FastAPI dependency guarding every /api/algo/* route except login.

    503 when auth is not configured at all (mirrors the fixed-gate pattern:
    misconfiguration must be loud, never an open door); 401 otherwise.

    NOTE ON NAMING: this verifies *authentication* (any signed-in role may
    read). Role enforcement (§11.2) layers on top via ``require_editor`` /
    ``require_admin_role`` below.
    """
    if not await auth_configured():
        raise HTTPException(
            503,
            "Algo admin login is not configured. Set ALGO_ADMIN_USER and "
            "ALGO_ADMIN_PASSWORD in your .env file and restart the backend to "
            "create the first admin; after that the credentials live in the "
            "database and the .env values can be removed.",
        )
    token = request.cookies.get(SESSION_COOKIE, "")
    user_id = verify_token(token) if token else None
    if user_id is None:
        raise HTTPException(401, "Not signed in to Algo Config.")
    ident = await _identity_from_user_id(user_id)
    if ident is None:
        raise HTTPException(401, "Session user no longer exists.")
    return ident


async def require_editor(request: Request) -> AdminIdentity:
    """§11.2 — mutations need at least the editor role; a viewer is strictly
    read-only."""
    ident = await require_admin(request)
    if role_rank(ident.role) < 1:
        raise HTTPException(403, "Viewer role is read-only — ask the admin.")
    return ident


async def require_admin_role(request: Request) -> AdminIdentity:
    """§11.2 — kill-switch authority (and equally destructive actions) is
    reserved for the admin so rapid manual intervention always has one clear
    owner."""
    ident = await require_admin(request)
    if role_rank(ident.role) < 2:
        raise HTTPException(
            403, "Only the admin may do this (kill-switch authority, §11.2)."
        )
    return ident
