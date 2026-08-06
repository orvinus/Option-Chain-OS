"""Regression tests for the WS-vs-poller data-freshness split.

The feed watchdog's health check reads a "last flush" timestamp. The universe
poller enqueues its REST ticks onto the SAME queue as the live socket, so with the
poller on, REST rows kept that timestamp fresh while the WebSocket was stone dead —
the watchdog was structurally blind to exactly the outage class it was built for.

The fix: ticks carry an ``origin``; the aggregator's flush hook reports how many
flushed rows were WS-origin; ``rt.last_ws_flush_at`` advances only on those. The
steward keys on the WS timestamp; overall staleness (strict health) keys on the
any-origin one.

Also covered here: the in-bucket precedence rule (a REST tick may never displace a
live WS tick in the same bucket) and the open-bucket cap (a DB outage must not
accrete buckets until the OOM killer kills the feed too).

Runnable without pytest:  PYTHONPATH=. python tests/test_freshness_split.py
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from app.ingest import aggregator as agg_mod
from app.ingest.aggregator import MinuteAggregator
from app.ingest.types import Tick


def _tick(token: str, ts: datetime, origin: str = "ws", oi: int = 100) -> Tick:
    return Tick(
        ts=ts, token=token, symbol="NIFTY", expiry=date(2026, 8, 11), strike=24600,
        option_type="CE", ltp=10.0, oi=oi, volume=1, underlying=24600.0, origin=origin,
    )


class _FakeScope:
    """Stands in for session_scope(); records executed statements."""

    def __init__(self, log: list):
        self._log = log

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, rows):
        self._log.append(rows)


async def _flush_through(ticks: list[Tick]) -> tuple[list, list[tuple[datetime, int, int]]]:
    """Push ticks through a real aggregator with a fake DB; return (db_rows, hook_calls)."""
    q: asyncio.Queue = asyncio.Queue()
    hook_calls: list[tuple[datetime, int, int]] = []

    async def hook(bucket: datetime, rows: int, ws_rows: int) -> None:
        hook_calls.append((bucket, rows, ws_rows))

    db_log: list = []
    orig_scope = agg_mod.session_scope
    agg_mod.session_scope = _FakeScope(db_log)  # type: ignore[assignment]
    try:
        aggregator = MinuteAggregator(q, on_flush=hook)
        for t in ticks:
            aggregator._absorb(t)
        await aggregator._flush_closed(
            datetime.now(timezone.utc) + timedelta(minutes=5), force_all=True
        )
    finally:
        agg_mod.session_scope = orig_scope  # type: ignore[assignment]
    return db_log, hook_calls


# --------------------------------------------------------------------------- 1
async def test_poller_rows_do_not_count_as_ws_rows() -> None:
    """The masking regression: a poller-only flush must report ws_rows == 0."""
    ts = datetime(2026, 8, 6, 5, 0, 0, tzinfo=timezone.utc)
    _, hook_calls = await _flush_through([
        _tick("t1", ts, origin="poller"),
        _tick("t2", ts, origin="poller"),
    ])
    assert len(hook_calls) == 1, hook_calls
    _, rows, ws_rows = hook_calls[0]
    assert rows == 2, rows
    assert ws_rows == 0, (
        f"poller rows counted as WS rows ({ws_rows}) — the steward's WS-freshness "
        "signal would be advanced by REST failover data, masking a dead socket."
    )


# --------------------------------------------------------------------------- 2
async def test_ws_rows_counted_in_mixed_flush() -> None:
    ts = datetime(2026, 8, 6, 5, 0, 0, tzinfo=timezone.utc)
    _, hook_calls = await _flush_through([
        _tick("t1", ts, origin="ws"),
        _tick("t2", ts, origin="poller"),
    ])
    _, rows, ws_rows = hook_calls[0]
    assert (rows, ws_rows) == (2, 1), (rows, ws_rows)


# --------------------------------------------------------------------------- 3
async def test_tick_origin_defaults_to_ws() -> None:
    """Every existing Tick construction site (the ws_client) counts as WS."""
    t = Tick(
        ts=datetime.now(timezone.utc), token="x", symbol="NIFTY",
        expiry=date(2026, 8, 11), strike=24600, option_type="CE",
        ltp=1.0, oi=1, volume=0,
    )
    assert t.origin == "ws"


# --------------------------------------------------------------------------- 4
async def test_poller_tick_cannot_displace_ws_tick_in_same_bucket() -> None:
    """A stale REST quote with a fresher enqueue-ts must not overwrite the socket's
    value for the same (token, bucket) — the post-recovery linger hazard."""
    ts = datetime(2026, 8, 6, 5, 0, 0, tzinfo=timezone.utc)
    db_log, _ = await _flush_through([
        _tick("t1", ts, origin="ws", oi=111),
        _tick("t1", ts + timedelta(milliseconds=500), origin="poller", oi=999),
    ])
    assert len(db_log) == 1 and len(db_log[0]) == 1
    assert db_log[0][0]["oi"] == 111, (
        f"persisted oi={db_log[0][0]['oi']} — the poller tick displaced the WS tick"
    )


# --------------------------------------------------------------------------- 5
async def test_ws_tick_displaces_poller_tick_in_same_bucket() -> None:
    # Both ticks inside the same bucket regardless of PERSIST_BUCKET (1s..1min):
    # 30.000s and 30.500s share the second AND the minute.
    ts = datetime(2026, 8, 6, 5, 0, 30, tzinfo=timezone.utc)
    db_log, _ = await _flush_through([
        _tick("t1", ts + timedelta(milliseconds=500), origin="poller", oi=999),
        # Even with an OLDER timestamp, the live socket's value wins the bucket.
        _tick("t1", ts, origin="ws", oi=111),
    ])
    assert len(db_log[0]) == 1, db_log[0]
    assert db_log[0][0]["oi"] == 111, db_log[0][0]["oi"]


# --------------------------------------------------------------------------- 6
async def test_open_buckets_bounded_under_db_outage() -> None:
    """Flush failures leave buckets in place; the cap must bound the backlog."""
    q: asyncio.Queue = asyncio.Queue()
    aggregator = MinuteAggregator(q)
    orig_cap = agg_mod.MAX_OPEN_BUCKETS
    agg_mod.MAX_OPEN_BUCKETS = 100
    try:
        base = datetime(2026, 8, 6, 5, 0, 0, tzinfo=timezone.utc)
        for i in range(500):
            aggregator._absorb(_tick(f"t{i}", base + timedelta(seconds=i)))
        assert len(aggregator._open_buckets) <= 100 + 1, len(aggregator._open_buckets)
        assert aggregator._dropped_buckets > 0
    finally:
        agg_mod.MAX_OPEN_BUCKETS = orig_cap


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
