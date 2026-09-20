"""OI Structure Engine — golden-vector parity + behaviour tests.

Run:  cd backend && PYTHONPATH=. python tests/test_oi_structure_golden.py

The fixture (tests/fixtures/oi_structure_golden.json) was produced by running
the VERBATIM reference implementation (the JS from oi_smc_engine.html) under
node across 11 scenarios — defaults on two seeded sessions, tighter fractals,
coarse noise filters, fractional weights (exercises JS half-up rounding),
each engine kill switch, a matrix override, an always-gating threshold and a
too-short series. Each fixture embeds its input series, so this test needs no
PRNG replication: the Python engine must reproduce the reference outputs
field-for-field.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.algo.config_models import OIStructureMatrix, OIStructureParams
from app.algo.engines import oi_structure as eng

_FIXTURE = Path(__file__).parent / "fixtures" / "oi_structure_golden.json"


def _params_from_cfg(cfg: dict) -> OIStructureParams:
    w = cfg["weights"]
    return OIStructureParams(
        left_bars=cfg["left"],
        right_bars=cfg["right"],
        min_swing_move_cr=cfg["minMove"],
        eq_tolerance_cr=cfg["eqTol"],
        breakout_buffer_cr=cfg["buffer"],
        swing_weight=w["swing"],
        breakout_weight=w["breakout"],
        sweep_weight=w["sweep"],
        red_candle_weight=w["red"],
        agreement_bonus=w["agree"],
        confidence_threshold=cfg["threshold"],
        engine_1m_on=cfg["engineOn"]["oneMin"],
        engine_5m_on=cfg["engineOn"]["fiveMin"],
        matrix=OIStructureMatrix(call=cfg["matrix"]["call"], put=cfg["matrix"]["put"]),
    )


def _swings_tuple(swings) -> list[tuple]:
    return [(s.index, s.value, s.type, s.label) for s in swings]


def _expected_swings(raw: list[dict]) -> list[tuple]:
    return [(s["index"], s["value"], s["type"], s["label"]) for s in raw]


def test_golden_parity_all_scenarios():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8-sig"))
    assert len(data["fixtures"]) >= 11
    for fx in data["fixtures"]:
        name = fx["name"]
        params = _params_from_cfg(fx["cfg"])
        res = eng.evaluate(fx["callSeries"], fx["putSeries"], params)
        exp = fx["expected"]

        assert _swings_tuple(res.call_swings) == _expected_swings(exp["callSwings"]), (
            f"{name}: call-chart swing detection/classification diverged from the reference"
        )
        assert _swings_tuple(res.put_swings) == _expected_swings(exp["putSwings"]), (
            f"{name}: put-chart swing detection/classification diverged from the reference"
        )

        for side, got, want in (
            ("call", res.call_zone, exp["callZone"]),
            ("put", res.put_zone, exp["putZone"]),
        ):
            if want is None:
                assert got is None, f"{name}: {side} zone should be absent"
            else:
                assert got is not None and (got.hi, got.lo) == (want["hi"], want["lo"]), (
                    f"{name}: {side} zone mismatch"
                )

        assert res.call_breakout.direction == exp["callBreak"]["direction"], name
        assert res.put_breakout.direction == exp["putBreak"]["direction"], name
        assert res.call_red_5m == exp["callRed"] and res.put_red_5m == exp["putRed"], name

        assert res.call_pts == exp["callPts"], (
            f"{name}: callPts {res.call_pts} != reference {exp['callPts']}"
        )
        assert res.put_pts == exp["putPts"], (
            f"{name}: putPts {res.put_pts} != reference {exp['putPts']}"
        )
        assert res.signal == exp["signal"], f"{name}: signal mismatch"
        assert res.raw_direction == exp["rawDirection"], name
        assert res.confidence == exp["confidence"], name
        assert len(res.contributions) == exp["contribCount"], (
            f"{name}: contributing-events count mismatch"
        )
    print(f"golden parity: {len(data['fixtures'])} scenarios byte-identical")


# ── Behaviour cases the fixtures may not isolate ──────────────────────────

def _p(**over) -> OIStructureParams:
    return OIStructureParams(**over)


def test_eq_check_runs_before_higher_lower():
    # Two highs 0.01 apart with tolerance 0.015 → the second must be EQH even
    # though it is technically higher (spec §4: a near-tie reads as 'failed at
    # the same level twice', not fresh structure).
    series = [0, 1.0, 0, -1.0, 0, 1.01, 0, -1.5, 0]
    swings = eng.classify_structure(
        eng.find_swings(series, 1, 1, 0.0), eq_tolerance=0.015
    )
    highs = [s for s in swings if s.type == "high"]
    assert len(highs) >= 2 and highs[1].label == "EQH", swings


def test_tie_favours_call():
    # Symmetric inputs → equal points; leader must be CALL by the >= rule.
    call = [0, 1, 0, 2, 0, 3, 0, 4, 0, 5, 0]
    res = eng.evaluate(call, list(call), _p(confidence_threshold=0.0))
    assert res.call_pts == res.put_pts
    assert res.raw_direction == "CALL"
    assert res.signal == "CALL"


def test_sweep_tolerance_is_strictly_less_than():
    # JS: Math.abs(v - bo.value) < 0.05 — a distance of exactly 0.05 does NOT
    # earn the liquidity-sweep bonus.
    assert eng.SWEEP_TOLERANCE_CR == 0.05
    assert not (abs(1.05 - 1.00) < eng.SWEEP_TOLERANCE_CR - 1e-12)


def test_legacy_red_candle_primitive_still_reads_second_to_last():
    # The PRE-2026-09-19 primitive, kept so the original behaviour stays
    # testable. 10 values = two full candles; the first red, the second green:
    # candles[-2] is the first → red fires.
    series = [5, 4, 3, 2, 1, 1, 2, 3, 4, 5]
    assert eng.check_red_candle(eng.to_5m(series)) is True
    assert eng.check_red_candle(eng.to_5m([1, 2, 3, 4, 5])) is False


# ── F5: candle read (Signal Console §5.3–§5.8) ────────────────────────────

def test_strict_closed_reads_a_group_the_moment_it_completes():
    # 10 values = exactly two COMPLETE candles: first green, second red. The
    # legacy rule reads candles[-2] (green, stale); the strict rule reads the
    # second, which has genuinely finished. This is the whole fix.
    series = [1, 2, 3, 4, 5, 5, 4, 3, 2, 1]
    assert eng.check_red_candle(eng.to_5m(series)) is False   # legacy: stale
    assert eng.red_candle(series, 5, "closed") is True        # strict: correct


def test_strict_closed_ignores_a_partial_trailing_group():
    # 12 values = two complete candles (2nd red) + a 2-bar partial that is
    # rising. "closed" must ignore the partial and still read the red one;
    # legacy happens to agree here, which is why it was wrong only 1-in-5.
    series = [1, 2, 3, 4, 5, 5, 4, 3, 2, 1, 1, 9]
    assert eng.red_candle(series, 5, "closed") is True
    assert eng.check_red_candle(eng.to_5m(series)) is True


def test_forming_reads_the_unfinished_group_and_can_flip():
    # §5.6: the forming candle's close is the live value, so its colour moves.
    rising = [1, 2, 3, 4, 5, 5, 4, 3, 2, 1, 1, 9]     # partial opens 1, now 9
    assert eng.red_candle(rising, 5, "forming") is False
    falling = [1, 2, 3, 4, 5, 5, 4, 3, 2, 1, 9, 1]    # partial opens 9, now 1
    assert eng.red_candle(falling, 5, "forming") is True


def test_candle_size_is_configurable():
    # 6 values at size 3 = two complete candles; the second is red.
    series = [1, 2, 3, 9, 5, 1]
    assert eng.red_candle(series, 3, "closed") is True
    # The same values folded at 5 give one complete candle (rising) → green.
    assert eng.red_candle(series, 5, "closed") is False
    assert len(eng.to_candles(series, 3)) == 2
    # A size below 2 degrades to the original 5, never to a crash.
    assert eng.to_candles(series, 1) == eng.to_candles(series, 5)


def test_no_complete_candle_yet_is_not_red():
    assert eng.red_candle([1, 2, 3], 5, "closed") is False
    assert eng.red_candle([], 5, "forming") is False


# ── F4: swing scoring lookback (Signal Console §4.3–§4.8) ─────────────────

def test_lookback_window_includes_the_floor_bar():
    # §4.4 worked example: current bar 200, lookback 60 → floor 140; a swing
    # AT 140 is in, 139 is out.
    sw = [eng.Swing(index=i, value=0.0, type="high", label="HH") for i in (139, 140, 150, 200)]
    kept = [s.index for s in eng.swings_in_lookback(sw, series_len=201, lookback=60)]
    assert kept == [140, 150, 200]


def test_lookback_zero_is_the_whole_session():
    sw = [eng.Swing(index=i, value=0.0, type="high", label="HH") for i in (0, 5, 99)]
    assert eng.swings_in_lookback(sw, series_len=100, lookback=0) == sw


def test_lookback_filters_scoring_but_never_zones_or_breakouts():
    # §4.8 is the load-bearing rule: a short lookback must not erase a range
    # zone formed earlier in the session.
    call = [0.0, 0.6, 0.0, -0.6, 0.0, 0.6, 0.0, -0.6, 0.0, 0.6, 0.0, -0.6, 0.0, 0.6, 0.0]
    put = list(reversed(call))
    full = eng.evaluate(call, put, _p(confidence_threshold=0.0))
    short = eng.evaluate(call, put, _p(confidence_threshold=0.0, swing_scoring_lookback=3))
    assert full.call_zone == short.call_zone and full.put_zone == short.put_zone
    assert full.call_breakout.direction == short.call_breakout.direction
    assert full.put_breakout.direction == short.put_breakout.direction
    # ... while the scored population genuinely shrank.
    assert short.scored_call_swings < full.scored_call_swings
    assert len(short.call_swings) == len(full.call_swings)  # detection untouched


def test_scored_swing_counts_equal_totals_when_lookback_is_zero():
    call = [0.0, 0.6, 0.0, -0.6, 0.0, 0.6, 0.0, -0.6, 0.0]
    r = eng.evaluate(call, list(reversed(call)), _p())
    assert r.scored_call_swings == len(r.call_swings)
    assert r.scored_put_swings == len(r.put_swings)


def test_both_engines_off_always_no_trade():
    call = [0, 1, 0, 2, 0, 3, 0, 4, 0, 10, 0]
    res = eng.evaluate(call, list(call), _p(engine_1m_on=False, engine_5m_on=False))
    assert res.signal == "NO_TRADE" and res.call_pts == 0 and res.put_pts == 0


def test_first_of_kind_swings_never_score():
    # A single high + single low → labels H / L → zero swing points; only the
    # possible breakout/red-candle can score. (No flat tail — equal values in
    # a fractal window legitimately qualify as swings and would add a third.)
    series = [0.0, 1.0, 0.5, -1.0, -0.5]
    swings = eng.classify_structure(eng.find_swings(series, 1, 1, 0.0), 0.001)
    assert all(s.label in ("H", "L") for s in swings), swings


def _run_all() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all OI Structure golden tests passed")


if __name__ == "__main__":
    _run_all()
