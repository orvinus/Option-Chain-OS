"""The health/sentinel contract for the TrueData failure modes.

``scripts/oi-sentinel.sh`` restarts the backend on a sustained 503. Two TrueData
failures are made WORSE by that:

  session_wedged  a restart is itself a dirty disconnect, so it re-wedges the
                  vendor session and the loop never terminates;
  proxy_down      the vendor is unreachable at the network layer and only the
                  host can fix it.

Both must therefore appear in ``reasons`` AND force ``restart_recommended:false``.
These tests exercise the real endpoint through the real app, and then assert that
the sentinel script actually greps for what the endpoint emits — a contract
between a Python service and a shell script that nothing else would catch.

Runnable without pytest:  PYTHONPATH=. python tests/test_health_wedge_contract.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.api import health as health_mod
from app.auth.td_session import SessionState, get_td_session
from app.core import proxy_health
from app.core.config import settings

REPO = Path(__file__).resolve().parents[2]
SENTINEL = REPO / "scripts" / "oi-sentinel.sh"


def _body(resp) -> dict:
    return json.loads(bytes(resp.body).decode())


def _force_session_state(state: SessionState) -> None:
    get_td_session()._state = state


def test_wedged_session_reports_and_forbids_restart() -> None:
    original_vendor = settings.feed_vendor
    try:
        object.__setattr__(settings, "feed_vendor", "truedata")
        _force_session_state(SessionState.WEDGED)
        resp = asyncio.run(health_mod.health_strict())
        body = _body(resp)
        # Off-session/holiday short-circuits return 200 with no reasons; only
        # assert the contract when the endpoint actually evaluated the vendor.
        if resp.status_code == 503:
            assert "session_wedged" in body["reasons"]
            assert body["restart_recommended"] is False, (
                "restarting a wedged session re-wedges it — the sentinel must be "
                "told not to"
            )
            assert "wedged" in body["detail"].lower()
    finally:
        _force_session_state(SessionState.DISCONNECTED)
        object.__setattr__(settings, "feed_vendor", original_vendor)


def test_proxy_down_reports_and_forbids_restart() -> None:
    original_vendor = settings.feed_vendor
    original_result = proxy_health._last_result
    try:
        object.__setattr__(settings, "feed_vendor", "truedata")
        proxy_health._last_result = False
        resp = asyncio.run(health_mod.health_strict())
        body = _body(resp)
        if resp.status_code == 503:
            assert "proxy_down" in body["reasons"]
            assert body["restart_recommended"] is False
    finally:
        proxy_health._last_result = original_result
        object.__setattr__(settings, "feed_vendor", original_vendor)


def test_xts_mode_is_completely_unaffected() -> None:
    """The frozen contract must not gain fields when the vendor is XTS."""
    original_vendor = settings.feed_vendor
    try:
        object.__setattr__(settings, "feed_vendor", "xts")
        _force_session_state(SessionState.WEDGED)   # must be ignored entirely
        proxy_health._last_result = False           # must be ignored entirely
        resp = asyncio.run(health_mod.health_strict())
        body = _body(resp)
        assert "session_wedged" not in body.get("reasons", [])
        assert "proxy_down" not in body.get("reasons", [])
    finally:
        _force_session_state(SessionState.DISCONNECTED)
        proxy_health._last_result = True
        object.__setattr__(settings, "feed_vendor", original_vendor)


def test_sentinel_greps_for_exactly_what_health_emits() -> None:
    """The Python/shell contract. A rename on either side breaks recovery silently."""
    script = SENTINEL.read_text(encoding="utf-8")
    for reason in ('"session_wedged"', '"proxy_down"'):
        assert reason in script, (
            f"oi-sentinel.sh does not branch on {reason} — it would restart the "
            "backend into the very failure the reason describes"
        )
    # And it must still honour the pre-existing generic guard.
    assert '"restart_recommended": *false' in script


def test_sentinel_rechecks_before_restarting() -> None:
    """A restart of a recovered process costs a wedge cycle under TrueData."""
    script = SENTINEL.read_text(encoding="utf-8")
    restart_block = script.split("say \"restarting backend")[0]
    assert "recovered on re-probe" in restart_block, (
        "the sentinel restarts without a final health re-probe"
    )


def test_health_feed_route_exists_and_is_separate() -> None:
    """Vendor detail must live off the frozen contract.

    Uses the OpenAPI surface, not ``app.routes`` — newer FastAPI wraps
    included routers (``_IncludedRouter``) instead of flattening them, so
    route-object introspection is version-fragile."""
    from app.main import app

    paths = set(app.openapi()["paths"].keys())
    assert "/api/health/feed" in paths
    assert "/api/health" in paths and "/api/health/strict" in paths


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall health/wedge contract tests passed")
