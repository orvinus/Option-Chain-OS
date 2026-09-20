"""/seceretdashboard — the REAL Algo Config + Backtesting UI, read-only.

WHY A REDIRECT AND NOT A COPY
A hand-built mirror can only ever be an approximation: it drifts the moment a
field is added upstream, and "same to same" then quietly stops being true. So
this route does not render anything of its own. It mints a **viewer** session
and hands the visitor the actual application — the same bundle, the same
components, the same tabs. There is nothing to keep in sync because there is no
second implementation. Any change made in Algo Config or Backtesting is visible
here the instant it is saved, because it IS that dashboard.

HOW READ-ONLY IS ENFORCED — SERVER SIDE, NOT BY HIDING BUTTONS
The RBAC this depends on already existed (algo/auth.py):

    _ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}
    require_editor     -> 403 "Viewer role is read-only — ask the admin."
    require_admin_role -> 403 admin-only (kill switches)

`require_admin` (authentication only) still allows every GET, so the viewer
reads everything. Every mutating route already depends on require_editor or
require_admin_role, so a viewer cannot save a config, start a backtest, flip a
kill switch or place an order — even by calling the API directly with the
cookie. This route therefore adds a session, never a new privilege path.

WHAT STILL LEAKS
The URL is the only gate, and URLs leak (nginx access.log, browser history,
Referer). What is exposed is strategy configuration, backtests and P&L — not a
way in: no credentials are shown, and nothing can be changed. Remove the wiring
in main.py / nginx.conf to switch it off.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from ..algo.auth import SESSION_COOKIE, hash_password, issue_token
from ..core.config import settings
from ..core.db import AsyncSessionLocal

router = APIRouter(tags=["dharmik"])

# The viewer identity. Its password is random and thrown away: nobody signs in
# as this user — the session is minted server-side by the route below.
VIEWER_USERNAME = "dharmik-viewer"

_FIND = text("SELECT user_id, role FROM algo_users WHERE username = :u")
_MAKE = text(
    "INSERT INTO algo_users (username, password_hash, role) "
    "VALUES (:u, :p, 'viewer') RETURNING user_id"
)
_DEMOTE = text("UPDATE algo_users SET role = 'viewer' WHERE user_id = :uid")


async def _viewer_user_id() -> int | None:
    """The viewer's user_id, creating the row on first use.

    Re-asserts role='viewer' every time. If this row were ever edited to
    'admin' in the database, this URL would silently become a full-privilege
    door; pinning it on each request makes that impossible to do by accident.
    """
    try:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(_FIND, {"u": VIEWER_USERNAME})).mappings().first()
            if row is None:
                uid = (await s.execute(_MAKE, {
                    "u": VIEWER_USERNAME,
                    "p": hash_password(secrets.token_urlsafe(32)),
                })).scalar_one()
            else:
                uid = int(row["user_id"])
                if row["role"] != "viewer":
                    await s.execute(_DEMOTE, {"uid": uid})
            await s.commit()
            return int(uid)
    except Exception:
        return None


@router.get("/seceretdashboard", include_in_schema=False)
async def dharmik_secret_dashboard() -> RedirectResponse:
    uid = await _viewer_user_id()
    # 303 so the browser follows with GET, and no-store so a shared/proxy cache
    # can never hand this redirect (and its Set-Cookie) to somebody else.
    # ?mirror=1 marks THIS entry point. The marker only decides what the UI
    # shows; the viewer COOKIE is what decides what may be read or written.
    # Faking the marker therefore buys nothing: without the cookie every algo
    # call still 401s. Keeping them separate is what leaves the plain
    # dashboard at / completely untouched.
    resp = RedirectResponse(url="/?mirror=1", status_code=303)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if uid is None:
        return resp

    resp.delete_cookie(SESSION_COOKIE, path="/api/algo")
    resp.set_cookie(
        SESSION_COOKIE,
        issue_token(uid),
        max_age=int(settings.algo_session_ttl_h * 3600),
        httponly=True,
        samesite="lax",
        secure=settings.log_format == "json",
        # Site-wide, matching the normal login: the live stream handshakes at
        # /ws/algo-stream and a path-scoped cookie is simply not sent there.
        path="/",
    )
    return resp


# ── HOW TO ENABLE / DISABLE ────────────────────────────────────────────────
# 1. backend/app/main.py:  app.include_router(dharmik.router)   (outside api_router)
# 2. docker/nginx.conf:    location = /seceretdashboard { proxy_pass http://backend:8000; }
# Remove either one to switch the page off. Deleting the dharmik-viewer row
# from algo_users revokes every session this route has ever issued.
