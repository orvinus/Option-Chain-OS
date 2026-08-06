"""Regression tests for the token lifecycle: validated restore + no-logout stop.

Both outages traced back to token lifecycle mistakes:
  * restore trusted any <24h-old stored row on pure TTL arithmetic — including a
    token the previous shutdown had itself logged out at the broker — so the
    process ran whole sessions on a corpse while health said authenticated=true;
  * graceful shutdown logged the token out, guaranteeing the NEXT boot restored
    that corpse (its row stayed newest in auth_sessions).

(The daily-08:35 rotation schedule is tested in test_steward_ladder.py, where the
steward that owns it lives.)

Runnable without pytest:  PYTHONPATH=. python tests/test_token_schedule.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import app.market_data.xts_client as xc
from app.auth import market_session as ms_mod
from app.auth.market_session import MarketDataSession


def _make_sess() -> MarketDataSession:
    sess = MarketDataSession()

    async def noop_seed() -> None:
        return None

    sess._seed_floor_from_db_once = noop_seed  # type: ignore[assignment]
    return sess


class _FakeRestoreScope:
    """session_scope() stand-in serving one auth_sessions row."""

    def __init__(self, row):
        self._row = row

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql):  # restore only issues SELECTs here
        class _R:
            def __init__(self, row):
                self._row = row

            def mappings(self):
                return self

            def first(self):
                return self._row

        return _R(self._row)


def _fresh_row() -> dict:
    return {
        "client_code": "TESTUSER",
        "jwt_token": "stored-token",
        "refresh_token": "stored-token",
        "feed_token": "stored-token",
        "issued_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }


async def _restore_with(verdict, row=None) -> tuple[MarketDataSession, bool]:
    sess = _make_sess()

    async def fake_validate(token: str):
        return verdict

    orig_scope = ms_mod.session_scope
    orig_validate = xc.validate_token
    ms_mod.session_scope = _FakeRestoreScope(row or _fresh_row())  # type: ignore[assignment]
    xc.validate_token = fake_validate  # type: ignore[assignment]
    try:
        ok = await sess.try_restore_session_from_db()
    finally:
        ms_mod.session_scope = orig_scope  # type: ignore[assignment]
        xc.validate_token = orig_validate  # type: ignore[assignment]
    return sess, ok


# --------------------------------------------------------------------------- 1
async def test_restore_rejects_broker_dead_token() -> None:
    """THE regression: TTL-valid but broker-rejected must NOT restore."""
    sess, ok = await _restore_with(False)
    assert ok is False, "a broker-rejected token was restored on TTL arithmetic"
    assert sess._tokens is None, "the corpse must be dropped so a fresh login runs"


# --------------------------------------------------------------------------- 2
async def test_restore_accepts_validated_token() -> None:
    sess, ok = await _restore_with(True)
    assert ok is True
    assert sess.authenticated is True
    assert sess.token == "stored-token"


# --------------------------------------------------------------------------- 3
async def test_restore_keeps_token_on_inconclusive_validation() -> None:
    """Network trouble is not a verdict — keep the token (a fresh login would fail
    on the same network; the steward catches a truly dead token in minutes)."""
    sess, ok = await _restore_with(None)
    assert ok is True
    assert sess.token == "stored-token"


# --------------------------------------------------------------------------- 4
async def test_restore_drops_expired_token_without_probing() -> None:
    calls = {"n": 0}

    async def fake_validate(token: str):
        calls["n"] += 1
        return True

    row = _fresh_row()
    row["issued_at"] = datetime.now(timezone.utc) - timedelta(hours=30)
    sess = _make_sess()
    orig_scope = ms_mod.session_scope
    orig_validate = xc.validate_token
    ms_mod.session_scope = _FakeRestoreScope(row)  # type: ignore[assignment]
    xc.validate_token = fake_validate  # type: ignore[assignment]
    try:
        ok = await sess.try_restore_session_from_db()
    finally:
        ms_mod.session_scope = orig_scope  # type: ignore[assignment]
        xc.validate_token = orig_validate  # type: ignore[assignment]
    assert ok is False
    assert calls["n"] == 0, "an expired token needs no network probe"


# --------------------------------------------------------------------------- 5
async def test_stop_does_not_log_the_token_out() -> None:
    """Shutdown must leave the token valid for the successor process — logging it
    out guaranteed every restart restored a corpse (zero-login restarts are the
    payoff of the whole restore path)."""
    calls = {"n": 0}

    async def fake_logout(token: str) -> None:
        calls["n"] += 1

    sess = _make_sess()
    await sess.set_tokens("tok", "tok", "tok", "TESTUSER")
    orig = xc.logout
    xc.logout = fake_logout  # type: ignore[assignment]
    try:
        await sess.stop()
    finally:
        xc.logout = orig  # type: ignore[assignment]
    assert calls["n"] == 0, "stop() logged the token out — the next boot restores a corpse"


# --------------------------------------------------------------------------- runner
async def _main() -> int:
    import inspect

    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and inspect.iscoroutinefunction(v)
    ]
    passed = 0
    for t in tests:
        try:
            await t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
