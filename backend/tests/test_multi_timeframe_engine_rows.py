"""Main Multi-TF page rows = the engines' own series (2026-09-23).

The page used to diff live-table snapshots only, so a live-feed gap read as
zero change while Algo Config (archive-filled series) showed the real move.
``_engine_rows`` now builds the rows from ``mtf_pair_at`` — the exact path the
Algo Config panel uses — so both pages report identical numbers.

Run:  cd backend && PYTHONPATH=. python -m pytest -q tests/test_multi_timeframe_engine_rows.py
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime

import app.api.algo_engines as ae
import app.api.multi_timeframe as mt
from app.algo.series import OIChangePair
from app.core.time_utils import IST

TFS = ["1m", "3m", "full_day"]
EXP = date(2026, 9, 22)


def _pair() -> OIChangePair:
    # cumulative-since-open, whole units in the *_full_cr copy (Cr)
    return OIChangePair(
        timestamps=[f"2026-09-15T13:5{i}:00+05:30" for i in range(5)],
        call_change_cr=[0.0, 0.0, 0.0, 0.0, 0.0],          # the 0.01-Cr copy (unused)
        put_change_cr=[0.0, 0.0, 0.0, 0.0, 0.0],
        strike_min=23000, strike_max=24000, spot=23510.0,
        call_change_full_cr=[0.0, 0.0010000, 0.0020000, 0.0035000, 0.0036664],
        put_change_full_cr=[0.0, -0.0005000, -0.0010000, -0.0012000, -0.0022803],
    )


def _run(coro):
    return asyncio.run(coro)


def _setup(monkeypatch, pair):
    calls = []

    async def fake(sym, exp, window, date_s, cut):
        calls.append((sym, exp, window, date_s, cut))
        return pair

    monkeypatch.setattr(ae, "mtf_pair_at", fake)
    mt._ENGINE_CACHE.clear()
    return calls


def test_rows_are_the_engine_series_in_oi_units(monkeypatch) -> None:
    calls = _setup(monkeypatch, _pair())
    as_of = IST.localize(datetime(2026, 9, 15, 14, 0, 37))
    rows, atm = _run(mt._engine_rows("NIFTY", EXP, TFS, as_of, 10))
    by = {r.timeframe: r for r in rows}
    assert (by["1m"].call_oi_change, by["1m"].put_oi_change) == (1664, -10803)
    assert (by["3m"].call_oi_change, by["3m"].put_oi_change) == (26664, -17803)
    assert (by["full_day"].call_oi_change, by["full_day"].put_oi_change) == (36664, -22803)
    # an as-of instant resolves to its date + the minute (closed minutes only)
    assert calls == [("NIFTY", EXP, 10, "2026-09-15", datetime(2026, 1, 1, 14, 0).time())]
    assert atm == 23500, "ATM ± N names the centre of the basket actually summed"


def test_full_chain_uses_window_minus_one_and_no_atm_override(monkeypatch) -> None:
    calls = _setup(monkeypatch, _pair())
    _, atm = _run(mt._engine_rows("NIFTY", EXP, TFS, None, None))
    assert calls[0][2] == -1 and calls[0][3] is None and calls[0][4] is None
    assert atm is None


def test_no_engine_data_keeps_the_snapshot_rows(monkeypatch) -> None:
    _setup(monkeypatch, None)
    assert _run(mt._engine_rows("NIFTY", EXP, TFS, None, 10)) is None


def test_unsupported_timeframe_keeps_the_snapshot_rows(monkeypatch) -> None:
    calls = _setup(monkeypatch, _pair())
    assert _run(mt._engine_rows("NIFTY", EXP, ["1m", "45m"], None, 10)) is None
    assert calls == [], "never computed"


def test_one_computation_per_minute(monkeypatch) -> None:
    calls = _setup(monkeypatch, _pair())
    as_of = IST.localize(datetime(2026, 9, 15, 14, 0, 5))
    _run(mt._engine_rows("NIFTY", EXP, TFS, as_of, 10))
    _run(mt._engine_rows("NIFTY", EXP, TFS, IST.localize(datetime(2026, 9, 15, 14, 0, 50)), 10))
    assert len(calls) == 1, "same closed minute → served from cache"
    _run(mt._engine_rows("NIFTY", EXP, TFS, IST.localize(datetime(2026, 9, 15, 14, 1, 2)), 10))
    assert len(calls) == 2
