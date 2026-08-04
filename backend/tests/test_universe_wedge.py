"""Regression tests for the 2026-08-03/04 "feed permanently disconnected" outage.

Production served 13-hour-old data through a whole trading session while
``/api/health`` reported ``authenticated: true``. The observed state was
``feed_connected: false``, ``tokens_subscribed: 0``, ``expiries: []`` — i.e. the
option universe had gone empty and nothing could rebuild it.

Two independent defects had to line up, and each gets a test here:

1.  ``_load_fo_master_rows`` cached an EMPTY instrument master as "fresh". The
    reuse guard was ``_master_rows is not None``, which an empty list passes, so
    one bad fetch pinned an empty universe for the full 20h CACHE_TTL — past the
    end of the next session.
2.  ``_resubscribe_provider`` assigned that empty result straight onto the runtime,
    destroying a working subscription list (and emptying ``/api/expiries``).

Runnable without pytest:  PYTHONPATH=. python tests/test_universe_wedge.py
Also collectable by pytest (each ``test_*`` is an async function).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.market import scripmaster


# --------------------------------------------------------------------------- 1
async def test_empty_master_is_not_cached_as_fresh() -> None:
    """A dump with no F&O lines must raise and leave the previous cache intact."""
    calls = {"n": 0}

    async def fake_get_master(token, segments) -> str:
        calls["n"] += 1
        # What a broker error envelope actually degrades to: a body with no
        # NSEFO/BSEFO/MCXFO prefixed lines at all.
        return "Invalid Token"

    class FakeSession:
        authenticated = True
        token = "tok"

    orig_master = scripmaster.xts_client.get_master
    orig_rows, orig_at = scripmaster._master_rows, scripmaster._master_fetched_at
    import app.auth as auth_mod

    orig_sess = auth_mod.get_session_manager
    scripmaster.xts_client.get_master = fake_get_master  # type: ignore[assignment]
    auth_mod.get_session_manager = lambda: FakeSession()  # type: ignore[assignment]
    try:
        scripmaster._master_rows = None
        scripmaster._master_fetched_at = None

        raised = False
        try:
            await scripmaster._load_fo_master_rows()
        except RuntimeError:
            raised = True
        assert raised, "an empty master must RAISE, not be returned as a valid answer"
        assert scripmaster._master_rows is None, (
            "an empty master must NOT be cached — caching it pins an empty option "
            "universe for CACHE_TTL and wedges the live feed"
        )

        # And the very next call must actually retry the broker rather than serve
        # a cached empty list.
        try:
            await scripmaster._load_fo_master_rows()
        except RuntimeError:
            pass
        assert calls["n"] == 2, "a failed master fetch must be retried, not cached"
    finally:
        scripmaster.xts_client.get_master = orig_master  # type: ignore[assignment]
        auth_mod.get_session_manager = orig_sess  # type: ignore[assignment]
        scripmaster._master_rows, scripmaster._master_fetched_at = orig_rows, orig_at


async def test_stale_dated_cache_is_not_fresh() -> None:
    """A master fetched on a previous IST trading date must not be reused.

    New expiries and strikes are listed exactly across that boundary, so a TTL
    alone (20h) would happily serve yesterday's universe this morning.
    """
    assert scripmaster._cache_still_fresh(datetime.utcnow()) is True
    # 19:55 IST yesterday = 14:25 UTC yesterday: inside a 20h TTL this morning, but
    # a different IST trading date. This is exactly the outage's timing.
    yesterday = datetime.utcnow().replace(hour=14, minute=25) - scripmaster.timedelta(days=1)
    assert scripmaster._cache_still_fresh(yesterday) is False
    assert scripmaster._cache_still_fresh(None) is False


async def test_empty_payload_is_never_fresh() -> None:
    entry = scripmaster.ScripMasterCache(raw=[], fetched_at=datetime.utcnow())
    assert entry.fresh() is False, "an empty payload must never count as a fresh cache"
    entry_ok = scripmaster.ScripMasterCache(raw=[{"x": 1}], fetched_at=datetime.utcnow())
    assert entry_ok.fresh() is True


# --------------------------------------------------------------------------- 2
async def test_resubscribe_keeps_last_good_universe_on_empty_resolve() -> None:
    """An empty resolve must not wipe a working runtime universe."""
    import app.main as main_mod
    from app.runtime import get_runtime

    rt = get_runtime()
    sentinel_tokens = ["token-a", "token-b"]
    sentinel_expiries = [date(2026, 8, 4)]
    prev = (rt.tokens, rt.expiries, rt.latest_spot, rt.active_symbol)
    rt.tokens = sentinel_tokens  # type: ignore[assignment]
    rt.expiries = sentinel_expiries
    rt.latest_spot = 24774.3
    rt.active_symbol = "NIFTY"

    async def empty_resolver(*_a, **_kw):
        return [], []

    orig_resolve = main_mod.resolve_option_universe
    main_mod.resolve_option_universe = empty_resolver  # type: ignore[assignment]
    try:
        tokens, spot = await main_mod._resubscribe_provider()
        assert tokens == [], "the provider must report the empty resolve to its caller"
        assert rt.tokens == sentinel_tokens, (
            "an empty resolve must NOT overwrite a working subscription list — "
            "doing so is what produced tokens_subscribed:0 in production"
        )
        assert rt.expiries == sentinel_expiries, (
            "an empty resolve must NOT empty rt.expiries — /api/expiries and "
            "_expiry_utils read it and would report 'no contracts'"
        )
    finally:
        main_mod.resolve_option_universe = orig_resolve  # type: ignore[assignment]
        rt.tokens, rt.expiries, rt.latest_spot, rt.active_symbol = prev


# --------------------------------------------------------------------------- runner
async def _main() -> int:
    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and asyncio.iscoroutinefunction(v)
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
