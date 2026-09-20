"""TrueData realtime-session state machine.

THE INVERSION, which is the whole reason this file is not a port of
``market_session.py``:

    XTS       a new login KILLS the previous session. Relogin is the universal
              cure, so the danger is logging in TOO OFTEN — hence the 90s floor,
              the burst budget, the storm breaker, the boot-storm heuristic and
              the second-client detector (~380 lines of it).

    TrueData  a second login is REJECTED ("User Already Connected") and the live
              session SURVIVES. Relogin is therefore harmless to a healthy
              session and useless against a wedged one. The danger inverts: after
              a dirty disconnect the server still thinks we are connected, and
              the only way out is logoutRequest followed by a ~60s cool-down.
              Hammering reconnects during that window keeps restarting the clock.

Ported unmodified, the XTS machinery becomes a lockout generator. So the state
machine here is small and its only real job is to make sure we spend the wedge
cool-down WAITING rather than retrying:

    disconnected -> connecting -> live
                       |            |
                       v            v
                    wedged  <-  (dirty drop)
                       |
                  logout_request()
                       |
                       v
                   cooldown --(elapsed)--> disconnected

A graceful stop sends ``{"method":"logout"}`` on the socket instead, which
avoids the wedge entirely — the REST logoutRequest is the dirty-crash path only.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from enum import Enum

from ..core.config import settings
from ..core.logging import get_logger
from ..core.notify import notify

log = get_logger("td_session")

# The vendor documents "wait about a minute" after logoutRequest. 75s gives that
# a margin: coming back early costs another full wedge cycle, so the expected
# cost of waiting too long is far lower than the cost of waiting too little.
LOGOUT_COOLDOWN_S = 75.0

# Never fire logoutRequest more often than this. It is a heavy, session-
# destroying call; a loop of them is indistinguishable from an outage.
LOGOUT_MIN_INTERVAL_S = 90.0

# Consecutive rejections before we conclude we are wedged rather than merely
# racing our own previous connection. One rejection can happen legitimately
# during a fast reconnect; two in a row cannot.
REJECTIONS_TO_WEDGED = 2


class SessionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    LIVE = "live"
    WEDGED = "wedged"
    COOLDOWN = "cooldown"


class SessionCoolingDown(RuntimeError):
    """Raised when a connect is attempted inside the wedge cool-down."""

    def __init__(self, remaining_s: float) -> None:
        super().__init__(f"wedge cool-down active, {remaining_s:.0f}s remaining")
        self.remaining_s = remaining_s


class TrueDataSession:
    """Credentials holder + realtime-session state. One per process."""

    def __init__(self) -> None:
        self._state = SessionState.DISCONNECTED
        self._connect_lock = asyncio.Lock()
        self._cooldown_until: float = 0.0
        self._last_logout_at: float = 0.0
        self.consecutive_rejections: int = 0
        self.last_connect_at: datetime | None = None
        self.last_rejection_reason: str = ""
        self.wedge_recoveries: int = 0
        # Entitlements echoed by the vendor on every login. Re-read on each
        # connect: they are toggled server-side by support ticket and can change
        # under a running process.
        self.maxsymbols: int = 0
        self.subscription: str = ""
        self.segments: list[str] = []
        self.validity: str = ""

    # ---------------- credentials ----------------
    @property
    def user(self) -> str:
        return (settings.truedata_user or "").strip()

    @property
    def password(self) -> str:
        return (settings.truedata_password or "").strip()

    def require_credentials(self) -> None:
        if not self.user or not self.password:
            raise RuntimeError(
                "Missing TrueData credentials. Set TRUEDATA_USER and "
                "TRUEDATA_PASSWORD in .env (never in code, never in a URL that "
                "reaches a log)."
            )

    # ---------------- state ----------------
    @property
    def state(self) -> SessionState:
        # Cool-down expiry is time-based, so it is evaluated on read rather than
        # by a timer: no task to leak, and no window where the state is stale.
        if self._state is SessionState.COOLDOWN and time.monotonic() >= self._cooldown_until:
            self._state = SessionState.DISCONNECTED
        return self._state

    @property
    def authenticated(self) -> bool:
        """Kept for call-site compatibility with the XTS session manager."""
        return self.state is SessionState.LIVE

    @property
    def cooldown_remaining_s(self) -> float:
        if self.state is not SessionState.COOLDOWN:
            return 0.0
        return max(0.0, self._cooldown_until - time.monotonic())

    def connect_guard(self) -> asyncio.Lock:
        """Single-flight connect lock.

        Two concurrent connects wedge EACH OTHER: the second is rejected as
        "already connected" by the first, and under the rejection semantics that
        rejection is indistinguishable from a genuine wedge. Every connect path —
        supervisor, steward escalation, boot — must hold this.
        """
        return self._connect_lock

    # ---------------- transitions ----------------
    def mark_connecting(self) -> None:
        if self.state is SessionState.COOLDOWN:
            raise SessionCoolingDown(self.cooldown_remaining_s)
        self._state = SessionState.CONNECTING
        self.last_connect_at = datetime.now(timezone.utc)

    def mark_live(self, login: dict | None = None) -> None:
        # Defence in depth: the feed already refuses a `success: false` reply
        # before calling this, but a session must never be able to report LIVE
        # off a login the vendor explicitly refused. Getting that wrong once
        # cost an afternoon of "connected, maxsymbols=0, reconnect" thrashing
        # with no indication the credentials were the problem.
        if isinstance(login, dict) and login.get("success") is False:
            self.mark_rejected(str(login.get("message") or "login refused"))
            return
        was = self._state
        self._state = SessionState.LIVE
        self.consecutive_rejections = 0
        self.last_rejection_reason = ""
        if login:
            self._absorb_entitlements(login)
        if was is not SessionState.LIVE:
            log.info(
                "td_session.live",
                maxsymbols=self.maxsymbols,
                subscription=self.subscription,
                validity=self.validity,
            )

    def mark_rejected(self, reason: str) -> None:
        """A login the vendor refused. NOT the same as a network failure.

        Only genuine "already connected" style refusals count toward the wedge
        signature; a TCP reset or a proxy failure must not, or a network blip
        would trigger a 75-second cool-down for no reason.
        """
        self.consecutive_rejections += 1
        self.last_rejection_reason = reason[:200]
        if self.consecutive_rejections >= REJECTIONS_TO_WEDGED:
            if self._state is not SessionState.WEDGED:
                log.error(
                    "td_session.wedged",
                    rejections=self.consecutive_rejections,
                    reason=self.last_rejection_reason,
                )
                notify(
                    "td_session_wedged",
                    f"⚠️ TrueData session wedged after "
                    f"{self.consecutive_rejections} rejections: {reason[:120]}",
                )
            self._state = SessionState.WEDGED
        else:
            self._state = SessionState.DISCONNECTED

    def mark_disconnected(self, reason: str = "") -> None:
        """A drop that is NOT a vendor rejection (socket closed, proxy died)."""
        if self.state in (SessionState.COOLDOWN, SessionState.WEDGED):
            return  # do not downgrade a wedge into a plain disconnect
        self._state = SessionState.DISCONNECTED
        if reason:
            log.info("td_session.disconnected", reason=reason[:160])

    def _absorb_entitlements(self, login: dict) -> None:
        if not isinstance(login, dict):
            return
        # `success` is a BOOLEAN on this API, not a nested payload. The old
        # `login.get("success", login)` was written for a nested shape, so on a
        # real reply it evaluated to `False`, failed the isinstance check and
        # returned early — silently discarding maxsymbols/subscription/validity
        # on EVERY login, successful or not. That is why the entitlement fields
        # always logged empty. Only fall back to a nested dict if one is
        # actually there.
        nested = login.get("success")
        body = nested if isinstance(nested, dict) else login
        try:
            self.maxsymbols = int(body.get("maxsymbols") or 0)
        except (TypeError, ValueError):
            self.maxsymbols = 0
        self.subscription = str(body.get("subscription") or "")
        segs = body.get("segments") or []
        self.segments = [str(s).lower() for s in segs] if isinstance(segs, list) else []
        self.validity = str(body.get("validity") or "")

    def entitlement_problems(self) -> list[str]:
        """Contract checks run on EVERY connect, not just the first.

        Capabilities are toggled server-side by support ticket, so an entitlement
        can disappear under a running process. A tick-less plan silently degrades
        this from a 1-second product to a 1-minute one, which is exactly the sort
        of change that should stop the feed loudly rather than quietly halve the
        data's value.
        """
        problems: list[str] = []
        if self.subscription and "tick" not in self.subscription.lower():
            problems.append(
                f"subscription={self.subscription!r} does not include tick-level "
                "data; streaming bars cannot feed a 1-second product"
            )
        # NOT `if self.maxsymbols and self.maxsymbols <= 0` — that guard was
        # dead code: the truthiness test is False for exactly the value the
        # branch existed to catch, so "maxsymbols is zero" could never fire.
        if self.maxsymbols <= 0:
            problems.append("maxsymbols is zero — nothing can be subscribed")
        return problems

    # ---------------- wedge recovery ----------------
    async def logout_request(self, reason: str, force: bool = False) -> bool:
        """Clear a wedged session and enter the cool-down.

        Rate-limited: this is the heavy hammer, and firing it in a loop is worse
        than the wedge it treats. ``force`` bypasses the limiter for the
        pre-exit path only, where the process is going away regardless and
        leaving the session dirty guarantees the NEXT boot starts wedged.
        """
        now = time.monotonic()
        if not force and (now - self._last_logout_at) < LOGOUT_MIN_INTERVAL_S:
            log.info(
                "td_session.logout.rate_limited",
                since_last_s=round(now - self._last_logout_at, 1),
                reason=reason,
            )
            return False

        from ..market_data.truedata_rest import TrueDataRest

        self._last_logout_at = now
        self.wedge_recoveries += 1
        ok = False
        td = None
        try:
            td = TrueDataRest()
            ok = await td.logout_request(settings.truedata_ws_port)
        except Exception as e:
            log.warning("td_session.logout.error", error=str(e), reason=reason)
        finally:
            if td is not None:
                try:
                    await td.aclose()
                except Exception:
                    pass

        # Enter the cool-down whether or not the call succeeded: if it failed we
        # are certainly still wedged, and reconnecting immediately would only
        # confirm that at the cost of another cycle.
        self._cooldown_until = time.monotonic() + LOGOUT_COOLDOWN_S
        self._state = SessionState.COOLDOWN
        log.warning(
            "td_session.wedge_recovery",
            reason=reason, logout_ok=ok,
            cooldown_s=LOGOUT_COOLDOWN_S, recoveries=self.wedge_recoveries,
        )
        return ok

    def snapshot(self) -> dict:
        """Health payload. Additive only — never widens the frozen contract."""
        return {
            "state": self.state.value,
            "maxsymbols": self.maxsymbols,
            "subscription": self.subscription,
            "segments": self.segments,
            "validity": self.validity,
            "consecutive_rejections": self.consecutive_rejections,
            "cooldown_remaining_s": round(self.cooldown_remaining_s, 1),
            "wedge_recoveries": self.wedge_recoveries,
            "last_connect_at": (
                self.last_connect_at.isoformat() if self.last_connect_at else None
            ),
            "last_rejection_reason": self.last_rejection_reason or None,
        }


_singleton: TrueDataSession | None = None


def get_td_session() -> TrueDataSession:
    global _singleton
    if _singleton is None:
        _singleton = TrueDataSession()
    return _singleton
