"""Regression tests for the two replay-frame defects found on 2026-08-04.

Both were caught by computing the multi-timeframe grid from a real ``/api/replay``
payload and comparing it against ``/api/multi-timeframe?as_of=`` for the same
instant. The numbers were close but never equal, and the reasons were:

1.  **The player showed one step into the future.** ``time_bucket`` labels a bucket
    with its START, so the bucket labelled 11:00 holds the last tick in
    [11:00, 11:01). The clock read 11:00 while the chart showed data observed up to
    11:00:59. Proof from production: the engine at ``as_of=11:01:00`` returned
    ``totCE 90,647,245 / totPE 108,151,615`` — byte-identical to the replay frame
    labelled 11:00, while ``as_of=11:00:00`` returned different numbers entirely.

2.  **The session-open baseline was the wrong tick.** ``OIChangeEngine`` anchors
    "change today" on the FIRST tick at or after the open; replay derived it from
    the first gapfill bucket, which is that bucket's LAST tick. Put OI ramps hardest
    in the opening minute, so ``full_day`` read ~3.4% low (3,252,210 lots on the
    sample session).

Runnable without pytest:  PYTHONPATH=. python tests/test_replay_frames.py
Also collectable by pytest (each ``test_*`` is an async function).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from app.core.time_utils import IST
from app.services import replay as replay_mod

SYMBOL = "NIFTY"
EXPIRY = date(2026, 8, 4)
STRIKE = 24600
STEP = timedelta(minutes=1)


def _ist(hh: int, mm: int, ss: int = 0) -> datetime:
    return IST.localize(datetime(2026, 8, 4, hh, mm, ss))


# The book as the gapfill query returns it: one row per (bucket, strike, leg), the
# value being that bucket's LAST tick. Buckets are labelled by their start.
_SERIES_ROWS = [
    # bucket 09:15 -> last tick of the opening minute
    {"bucket": _ist(9, 15), "strike": STRIKE, "option_type": "CE", "oi": 150, "underlying": 24600.0, "last_ts": _ist(9, 15, 55)},
    {"bucket": _ist(9, 15), "strike": STRIKE, "option_type": "PE", "oi": 400, "underlying": 24600.0, "last_ts": _ist(9, 15, 55)},
    {"bucket": _ist(9, 16), "strike": STRIKE, "option_type": "CE", "oi": 160, "underlying": 24610.0, "last_ts": _ist(9, 16, 50)},
    {"bucket": _ist(9, 16), "strike": STRIKE, "option_type": "PE", "oi": 450, "underlying": 24610.0, "last_ts": _ist(9, 16, 50)},
    {"bucket": _ist(9, 17), "strike": STRIKE, "option_type": "CE", "oi": 170, "underlying": 24620.0, "last_ts": _ist(9, 17, 30)},
    {"bucket": _ist(9, 17), "strike": STRIKE, "option_type": "PE", "oi": 500, "underlying": 24620.0, "last_ts": _ist(9, 17, 30)},
]

# The session-open snapshot: each leg's FIRST tick at or after 09:15. Deliberately
# different from the opening bucket's close above — that difference IS defect 2.
_BASE_ROWS = [
    {"strike": STRIKE, "option_type": "CE", "oi": 100, "underlying": 24590.0, "ts": _ist(9, 15, 5)},
    {"strike": STRIKE, "option_type": "PE", "oi": 200, "underlying": 24590.0, "ts": _ist(9, 15, 5)},
]


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Answers the two queries `fetch_replay` issues, dispatching on the SQL object."""

    def __init__(self, seen: dict):
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params):
        if sql is replay_mod._REPLAY_SERIES_SQL:
            self._seen["series_params"] = params
            return _FakeResult(_SERIES_ROWS)
        if sql is replay_mod._SNAPSHOT_AT_OR_AFTER_BOUNDED_SQL:
            self._seen["base_params"] = params
            return _FakeResult(_BASE_ROWS)
        raise AssertionError(f"unexpected query: {sql}")


async def _run_fetch() -> tuple[list, dict]:
    seen: dict = {}
    orig = replay_mod.AsyncSessionLocal
    replay_mod.AsyncSessionLocal = lambda: _FakeSession(seen)  # type: ignore[assignment]
    try:
        frames = await replay_mod.fetch_replay(
            EXPIRY, _ist(9, 15), _ist(9, 18), STEP, symbol=SYMBOL,
        )
    finally:
        replay_mod.AsyncSessionLocal = orig  # type: ignore[assignment]
    return frames, seen


# --------------------------------------------------------------------------- 1
async def test_frame_label_is_the_bucket_end_not_its_start() -> None:
    """A frame labelled T must not contain an observation later than T."""
    frames, _ = await _run_fetch()
    labels = [f.ts for f in frames]

    assert len(frames) == 3, f"expected one frame per bucket, got {labels}"
    assert labels[0] == _ist(9, 16).isoformat(), (
        f"the 09:15 bucket carries ticks up to 09:15:59, so it must be labelled "
        f"09:16 — labelling it 09:15 shows the player one step of the FUTURE. "
        f"Got {labels[0]}"
    )
    assert labels == [_ist(9, 16).isoformat(), _ist(9, 17).isoformat(), _ist(9, 18).isoformat()], labels

    # The invariant stated directly: every observation folded into a frame is at or
    # before that frame's own label.
    newest_tick = {_ist(9, 15): _ist(9, 15, 55), _ist(9, 16): _ist(9, 16, 50), _ist(9, 17): _ist(9, 17, 30)}
    for bucket, frame in zip(sorted(newest_tick), frames):
        assert newest_tick[bucket] <= datetime.fromisoformat(frame.ts), (
            f"frame {frame.ts} contains a tick from {newest_tick[bucket]}"
        )


# --------------------------------------------------------------------------- 2
async def test_change_is_anchored_on_the_session_open_tick() -> None:
    """Baseline = the first tick at/after 09:15, not the opening bucket's close."""
    frames, seen = await _run_fetch()
    first = frames[0]

    assert first.total_call_oi == 150 and first.total_put_oi == 400, (first.total_call_oi, first.total_put_oi)
    assert first.total_call_oi_change == 50, (
        "150 (09:15 close) - 100 (09:15:05, the session's first tick) = 50. "
        f"Got {first.total_call_oi_change} — 0 means the opening bucket was used as "
        "its own baseline, which is what made replay disagree with the Multi-TF tab."
    )
    assert first.total_put_oi_change == 200, (
        f"400 - 200 = 200; got {first.total_put_oi_change}. Puts ramp hardest in the "
        "opening minute, which is why this leg drifted ~3.4% on the real session."
    )

    # ... and it stays anchored there as the replay advances.
    assert frames[-1].total_call_oi_change == 70, frames[-1].total_call_oi_change
    assert frames[-1].total_put_oi_change == 300, frames[-1].total_put_oi_change

    # Per-strike rows carry the same anchor, so frame totals == sum of per-strike.
    row = first.rows[0]
    assert (row.strike, row.call_oi_change, row.put_oi_change) == (STRIKE, 50, 200), row

    # The baseline query must be bounded by `end`, or a strike with no data this
    # session borrows a LATER day's first row as its "session open".
    assert seen["base_params"]["cutoff"] == _ist(9, 15), seen["base_params"]["cutoff"]
    assert seen["base_params"]["upper"] == _ist(9, 18).astimezone(seen["base_params"]["upper"].tzinfo), (
        seen["base_params"]["upper"]
    )


# --------------------------------------------------------------------------- 3
async def test_strike_missing_from_the_open_does_not_spike() -> None:
    """A leg with no session-open row reports 0 change, never its whole OI."""
    seen: dict = {}
    orig = replay_mod.AsyncSessionLocal

    class _NoBaseSession(_FakeSession):
        async def execute(self, sql, params):
            if sql is replay_mod._SNAPSHOT_AT_OR_AFTER_BOUNDED_SQL:
                return _FakeResult([])  # strike entered mid-session
            return await super().execute(sql, params)

    replay_mod.AsyncSessionLocal = lambda: _NoBaseSession(seen)  # type: ignore[assignment]
    try:
        frames = await replay_mod.fetch_replay(
            EXPIRY, _ist(9, 15), _ist(9, 18), STEP, symbol=SYMBOL,
        )
    finally:
        replay_mod.AsyncSessionLocal = orig  # type: ignore[assignment]

    assert frames[0].total_call_oi_change == 0, (
        f"a strike with no session-open row must fall back to its first-seen book "
        f"(0 change), not report all 150 lots as a spike. Got {frames[0].total_call_oi_change}"
    )
    assert frames[0].total_put_oi_change == 0, frames[0].total_put_oi_change
    # It still accrues normally from that first-seen anchor.
    assert frames[-1].total_call_oi_change == 20, frames[-1].total_call_oi_change
    assert frames[-1].total_put_oi_change == 100, frames[-1].total_put_oi_change


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
