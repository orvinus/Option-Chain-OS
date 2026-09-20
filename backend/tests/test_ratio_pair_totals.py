"""RatioPair chart extras (2026-09-09): ``total_call_oi`` / ``total_put_oi``
ride along the MQAE input pair for the Ratio-panel tooltip and Full-Day
mode. They must stay index-aligned with ``timestamps`` through every trim
``ratio_pair_from_points`` applies (forming-bucket drop, leading-gap drop,
hold-last interior fill) and must never change the engine lines.

DB-free: rows are built in memory exactly as ``fetch_oi_timeseries`` emits them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.algo.series import RatioPair, ratio_pair_from_points
from app.services.oi_timeseries import OITimeseriesPoint, _safe_ratio


def _pt(minute: int, ce: int, pe: int) -> OITimeseriesPoint:
    return OITimeseriesPoint(
        ts=f"2026-08-17T09:{15 + minute:02d}:00+05:30",
        total_call_oi=ce,
        total_put_oi=pe,
        ratio=_safe_ratio(ce, pe),
        pcr=_safe_ratio(pe, ce),
    )


def _now_after(minute: int) -> datetime:
    """A wall clock at which bucket ``minute`` is CLOSED (its end has passed)."""
    return datetime.fromisoformat(f"2026-08-17T09:{15 + minute + 1:02d}:00+05:30").astimezone(timezone.utc)


def test_totals_align_with_timestamps_on_a_clean_series():
    pts = [_pt(i, 1000 + 10 * i, 800 + 5 * i) for i in range(6)]
    pair = ratio_pair_from_points(pts, now_utc=_now_after(5), strike_min=24000, strike_max=25000, spot=24500.0)
    assert pair is not None
    assert len(pair.total_call_oi) == len(pair.total_put_oi) == len(pair.timestamps) == 6
    assert pair.total_call_oi == [1000 + 10 * i for i in range(6)]
    assert pair.total_put_oi == [800 + 5 * i for i in range(6)]
    # engine lines untouched
    assert pair.yellow_ratio == [p.ratio for p in pts]
    assert pair.green_pcr == [p.pcr for p in pts]


def test_forming_bucket_is_dropped_from_totals_too():
    pts = [_pt(i, 1000 + i, 900 + i) for i in range(4)]
    # now = 09:18:30 → bucket 3 (09:18) is still forming
    now = datetime.fromisoformat("2026-08-17T09:18:30+05:30").astimezone(timezone.utc)
    pair = ratio_pair_from_points(pts, now_utc=now, strike_min=0, strike_max=0, spot=None)
    assert pair is not None
    assert pair.timestamps == [p.ts for p in pts[:3]]
    assert pair.total_call_oi == [1000, 1001, 1002]
    assert pair.total_put_oi == [900, 901, 902]


def test_leading_gap_trims_totals_with_the_same_offset():
    # First two buckets have no put OI → ratio None → normalized_pair drops them.
    pts = [_pt(0, 1000, 0), _pt(1, 1010, 0)] + [_pt(i, 1000 + i, 500 + i) for i in range(2, 7)]
    pair = ratio_pair_from_points(pts, now_utc=_now_after(6), strike_min=0, strike_max=0, spot=None)
    assert pair is not None
    assert len(pair.green_pcr) == 5
    assert pair.timestamps == [p.ts for p in pts[2:]]
    assert pair.total_call_oi == [p.total_call_oi for p in pts[2:]]
    assert pair.total_put_oi == [p.total_put_oi for p in pts[2:]]


def test_interior_gap_keeps_lengths_aligned_via_hold_last():
    # A mid-session bucket with zero put OI yields no ratio; hold-last fills it,
    # so the point SURVIVES and its raw totals must stay in place.
    pts = [_pt(0, 1000, 500), _pt(1, 1100, 550), _pt(2, 1200, 0), _pt(3, 1300, 600)]
    pair = ratio_pair_from_points(pts, now_utc=_now_after(3), strike_min=0, strike_max=0, spot=None)
    assert pair is not None
    assert len(pair.timestamps) == len(pair.yellow_ratio) == len(pair.total_call_oi) == 4
    assert pair.total_put_oi == [500, 550, 0, 600]
    assert pair.yellow_ratio[2] == pair.yellow_ratio[1]  # held


def test_ratio_pair_kwargs_constructors_default_totals_empty():
    pair = RatioPair(timestamps=["t"], green_pcr=[1.0], yellow_ratio=[1.0],
                     strike_min=0, strike_max=0, spot=None)
    assert pair.total_call_oi == [] and pair.total_put_oi == []


def test_replay_truncation_slices_totals_like_the_lines():
    from dataclasses import replace

    pts = [_pt(i, 1000 + i, 900 + i) for i in range(10)]
    pair = ratio_pair_from_points(pts, now_utc=_now_after(9), strike_min=0, strike_max=0, spot=None)
    assert pair is not None
    n = 4
    cut = replace(
        pair,
        timestamps=pair.timestamps[:n],
        green_pcr=pair.green_pcr[:n],
        yellow_ratio=pair.yellow_ratio[:n],
        total_call_oi=pair.total_call_oi[:n],
        total_put_oi=pair.total_put_oi[:n],
    )
    assert len(cut.total_call_oi) == len(cut.timestamps) == n
    assert cut.total_put_oi == [900, 901, 902, 903]
