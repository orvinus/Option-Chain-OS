"""Admin auth endpoints for the Algo Config tab (`/api/algo/auth/*`).

Real server-side sessions (§11.4): login verifies against the hashed
``algo_users`` row and sets a signed httpOnly cookie; every other
``/api/algo/*`` route requires it via the ``require_admin`` dependency.
Every success AND failure is audited (§11.3).
"""
from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..algo.audit import audit
from ..algo.auth import (
    SESSION_COOKIE,
    AdminIdentity,
    CredentialError,
    auth_configured,
    authenticate,
    change_credentials,
    issue_token,
    require_admin,
)
from ..core.config import settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/algo/auth", tags=["algo-auth"])


class AlgoLoginRequest(BaseModel):
    username: str
    password: str


class AlgoIdentityResponse(BaseModel):
    username: str
    role: str


class ChangeCredentialsRequest(BaseModel):
    """The confirm fields are checked in the browser; the server needs each
    value once. Nothing here is ever logged or audited by value."""
    current_username: str
    current_password: str
    new_username: str
    new_password: str


@router.post("/login", response_model=AlgoIdentityResponse)
async def algo_login(body: AlgoLoginRequest, response: Response) -> AlgoIdentityResponse:
    if not await auth_configured():
        raise HTTPException(
            503,
            "Algo admin login is not configured. Set ALGO_ADMIN_USER and "
            "ALGO_ADMIN_PASSWORD in your .env file and restart the backend to "
            "create the first admin; after that the credentials live in the "
            "database and the .env values can be removed.",
        )
    ident = await authenticate(body.username, body.password)
    if ident is None:
        # Failed attempts matter as much as successes (§11.3) — record the
        # attempted username, never the password.
        await audit("login_failed", username=body.username, scope="algo")
        log.info("algo.auth.login_failed", username=body.username)
        raise HTTPException(401, "Invalid username or password.")

    token = issue_token(ident.user_id)
    # Clear the legacy path-scoped cookie first. Without this, a browser that
    # signed in before the widening below keeps sending BOTH (the old
    # /api/algo one and the new / one) on every /api/algo request, and which
    # token wins is parse-order luck — a stale one would 401 at random.
    response.delete_cookie(SESSION_COOKIE, path="/api/algo")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(settings.algo_session_ttl_h * 3600),
        httponly=True,
        samesite="lax",
        # Cookie ships over the same origin the SPA came from; Caddy fronts
        # HTTPS in production, plain HTTP locally — so `secure` follows the
        # environment instead of breaking local dev.
        secure=settings.log_format == "json",
        # Site-wide, NOT "/api/algo". The live stream handshakes at
        # /ws/algo-stream, and a path-scoped cookie is simply not sent there —
        # which is why every REST call authenticated fine while the socket was
        # rejected with "Not signed in to Algo Config".
        #
        # Path is hygiene here, not a security boundary: the cookie is
        # httpOnly (script can never read it) and same-origin, and any
        # same-origin page could already drive /api/algo requests regardless of
        # scoping. Widening it costs nothing and stops the next non-/api/algo
        # endpoint hitting the same trap.
        path="/",
    )
    await audit("login_success", user_id=ident.user_id, username=ident.username, scope="algo")
    log.info("algo.auth.login_success", username=ident.username)
    return AlgoIdentityResponse(username=ident.username, role=ident.role)


# A change attempt verifies a password, so it is a guessing surface even behind
# a valid session. Successive failures for one account back off; a success
# clears the counter. Process-local and deliberately small — the audit log is
# the durable record.
_FAIL_WINDOW_S = 60.0
_FAIL_LIMIT = 5
_change_fails: dict[int, list[float]] = {}


def _throttle_check(user_id: int) -> None:
    now = time.time()
    recent = [t for t in _change_fails.get(user_id, []) if now - t < _FAIL_WINDOW_S]
    _change_fails[user_id] = recent
    if len(recent) >= _FAIL_LIMIT:
        raise HTTPException(429, "Too many failed attempts. Wait a minute and try again.")


@router.post("/change-credentials", response_model=AlgoIdentityResponse)
async def algo_change_credentials(
    body: ChangeCredentialsRequest,
    response: Response,
    ident: AdminIdentity = Depends(require_admin),
) -> AlgoIdentityResponse:
    """Change the Algo Config / Backtesting sign-in ID and password.

    Scope: this pair gates Algo Config and Backtesting ONLY. The dashboard's
    own display gate is a separate, unrelated credential and is untouched.

    The new values are written to ``algo_users``, which is the source of truth,
    so they survive a backend restart. The session cookie is cleared on success
    so the new credentials are proven immediately rather than at the next
    expiry.
    """
    _throttle_check(ident.user_id)
    try:
        updated = await change_credentials(
            user_id=ident.user_id,
            current_username=body.current_username,
            current_password=body.current_password,
            new_username=body.new_username,
            new_password=body.new_password,
        )
    except CredentialError as e:
        _change_fails.setdefault(ident.user_id, []).append(time.time())
        await audit(
            "credentials_change_failed",
            user_id=ident.user_id, username=ident.username, scope="algo",
            detail={"reason": str(e)},
        )
        log.info("algo.auth.credentials_change_failed", username=ident.username)
        raise HTTPException(400, str(e)) from e

    _change_fails.pop(ident.user_id, None)
    # Old username -> new username is the useful audit trail. Passwords never
    # appear here, in either direction.
    await audit(
        "credentials_changed",
        user_id=updated.user_id, username=updated.username, scope="algo",
        field="username", old_value=ident.username, new_value=updated.username,
    )
    log.info("algo.auth.credentials_changed", username=updated.username)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE, path="/api/algo")
    return AlgoIdentityResponse(username=updated.username, role=updated.role)


@router.post("/logout")
async def algo_logout(
    response: Response, ident: AdminIdentity = Depends(require_admin)
) -> dict[str, str]:
    # Both paths: the current site-wide cookie and any legacy /api/algo one
    # still held by a browser that signed in before the widening.
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE, path="/api/algo")
    await audit("logout", user_id=ident.user_id, username=ident.username, scope="algo")
    return {"status": "ok"}


@router.get("/me", response_model=AlgoIdentityResponse)
async def algo_me(
    request: Request,
    response: Response,
    ident: AdminIdentity = Depends(require_admin),
) -> AlgoIdentityResponse:
    """Identity — and a silent, idempotent cookie-path migration.

    A browser that signed in while the cookie was scoped to ``/api/algo`` keeps
    authenticating REST calls perfectly, so the admin gate never re-prompts —
    and the ``/ws/algo-stream`` handshake keeps being rejected forever. Since
    the gate calls this on mount, re-pin the SAME token at path ``/`` here.

    Same value, same expiry (parsed from the token's own ``user.expires.sig``
    shape), so this is a scope widening and NOT a sliding session — the
    session still dies exactly when it was always going to.
    """
    tok = request.cookies.get(SESSION_COOKIE, "")
    if tok:
        try:
            _uid, exp_s, _sig = tok.split(".")
            remaining = int(float(exp_s) - time.time())
            if remaining > 0:
                response.delete_cookie(SESSION_COOKIE, path="/api/algo")
                response.set_cookie(
                    SESSION_COOKIE,
                    tok,
                    max_age=remaining,
                    httponly=True,
                    samesite="lax",
                    secure=settings.log_format == "json",
                    path="/",
                )
        except (ValueError, TypeError):
            # Malformed token would already have failed require_admin above;
            # never let a cookie-rewrite problem break the identity call.
            pass
    return AlgoIdentityResponse(username=ident.username, role=ident.role)
