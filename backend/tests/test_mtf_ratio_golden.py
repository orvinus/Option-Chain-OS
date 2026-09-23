"""MTF Ratio engine — golden-vector parity + behaviour tests.

Run:  cd backend && PYTHONPATH=. python tests/test_mtf_ratio_golden.py

Fixtures (tests/fixtures/mtf_ratio_golden.json) come from running the VERBATIM
reference logic (ratioObj / lowestSide / the previewRules loop from
MTF_Ratio_Dashboard) under node across 11 scenarios that pin down every
documented quirk: first-match-wins ordering, Neutral-passes-side-filter,
zero-Δ-fails-sign-filter, Above = factor >= target vs Below = factor < target
(exact-boundary behaviour on both), infinite factors from one-sided zero Δ,
missing timeframe rows, empty rules and multi-condition AND.

DELIBERATE DIVERGENCE (2026-09-13). A rule condition's side is now matched
against the **Lowest-OI Side** (the lower SIGNED Δ, the platform's dominant-side
rule) instead of the magnitude-based Ratio Side. The row maths are untouched, so
``test_row_dump_parity`` still holds the reference to the letter; only the rule
OUTCOMES can move. Both are kept in the fixture: ``expected`` is what the
reference JS produced, ``expected_lowest_side`` is what we intend now. Exactly
one of the eleven scenarios differs, and it is worked through by hand in
``test_lowest_side_filter_changes_only_where_the_two_sides_disagree``.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from app.algo.config_models import MtfCondition, MtfRatioParams, MtfRule
from app.algo.engines import mtf_ratio as eng

_FIXTURE = Path(__file__).parent / "fixtures" / "mtf_ratio_golden.json"


def _params_from_rules(raw_rules: list[dict], timeframes: list[str]) -> MtfRatioParams:
    return MtfRatioParams(
        timeframes=timeframes,
        rules=[
            MtfRule(
                name=r["name"],
                on=r["on"],
                out=r["out"],
                conditions=[MtfCondition(**c) for c in r["conditions"]],
            )
            for r in raw_rules
        ],
    )


def _one_side_rule(side: str) -> MtfRatioParams:
    """A single 1m rule whose ONLY facet is the side filter."""
    return MtfRatioParams(
        timeframes=["1m"],
        rules=[
            MtfRule(
                name="side rule", on=True, out="Call",
                conditions=[MtfCondition(timeframe="1m", side=side)],
            )
        ],
    )


def test_golden_parity_all_scenarios():
    """Rule outcomes under the CURRENT (Lowest-OI Side) filter.

    ``expected_lowest_side`` is the intended result; ``expected`` is retained as
    the historical reference-JS result so the divergence stays visible.
    """
    data = json.loads(_FIXTURE.read_text(encoding="utf-8-sig"))
    assert len(data["fixtures"]) >= 11
    diverged = []
    for fx in data["fixtures"]:
        rows = {d["tf"]: (float(d["call"]), float(d["put"])) for d in fx["data"]}
        params = _params_from_rules(fx["rules"], [d["tf"] for d in fx["data"]])
        res = eng.evaluate(rows, params)
        want = fx.get("expected_lowest_side") or fx["expected"]
        assert res.direction == want["direction"], (
            f"{fx['name']}: direction {res.direction} != expected {want['direction']}"
        )
        assert res.matched_rule == want["matched"], (
            f"{fx['name']}: matched rule {res.matched_rule!r} != {want['matched']!r}"
        )
        if want["direction"] != fx["expected"]["direction"]:
            diverged.append(fx["name"])
    # Pinned: the switch must move exactly the scenarios we worked through, so a
    # future edit that quietly moves more cannot pass unnoticed.
    assert diverged == ["seed_defaults"], f"unexpected divergence set: {diverged}"


def test_lowest_side_filter_changes_only_where_the_two_sides_disagree():
    """The one moving scenario, by hand.

    seed_defaults 1m: call Δ = -444,000, put Δ = -2,061,000.
      Ratio Side  = smaller |Δ|      -> |-444,000| < |-2,061,000| -> Call
      Lowest side = lower signed Δ   -> -444,000  > -2,061,000    -> Put
    Rule 1 "Massive Call Capitulation" pins 1m to Call, so under the signed rule
    it no longer matches and evaluation falls through to rule 2, which pins no
    side. Direction therefore moves Call -> Put.
    """
    r = eng.ratio_reading("1m", -444_000.0, -2_061_000.0)
    assert r.side == "Call", "magnitude rule picks the smaller |Δ|"
    assert r.lowest_side == "Put", "signed rule picks the lower value"

    data = json.loads(_FIXTURE.read_text(encoding="utf-8-sig"))
    fx = next(f for f in data["fixtures"] if f["name"] == "seed_defaults")
    assert fx["expected"]["direction"] == "Call"              # reference JS
    assert fx["expected_lowest_side"]["direction"] == "Put"   # intended now


def test_the_rule_filters_on_lowest_side_not_ratio_side():
    """A single row where the two definitions point opposite ways: the rule must
    follow the signed one."""
    rows = {"1m": (-444_000.0, -2_061_000.0)}          # ratio-side Call, lowest Put
    assert eng.evaluate(rows, _one_side_rule("Call")).matched_rule is None, (
        "a Call filter must fail, because the SIGNED lower side is Put"
    )
    assert eng.evaluate(rows, _one_side_rule("Put")).matched_rule == "side rule"


def test_neutral_still_passes_any_side_filter():
    """Carried over from the reference, but on the SIGNED side — so it needs the
    two deltas to be exactly equal, not merely equal in magnitude."""
    r = eng.ratio_reading("1m", 5.0, 5.0)
    assert r.lowest_side == "Neutral"
    assert eng.evaluate({"1m": (5.0, 5.0)}, _one_side_rule("Call")).matched_rule == "side rule"

    # The mirror image is Neutral by MAGNITUDE but not by sign: it must now fail.
    mirror = eng.ratio_reading("1m", 2.0, -2.0)
    assert mirror.side == "Neutral" and mirror.lowest_side == "Put"
    assert eng.evaluate({"1m": (2.0, -2.0)}, _one_side_rule("Call")).matched_rule is None


def test_row_dump_parity():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8-sig"))
    seed = {d["tf"]: (float(d["call"]), float(d["put"])) for d in data["fixtures"][0]["data"]}
    for want in data["rowDump"]:
        r = eng.ratio_reading(want["tf"], *seed[want["tf"]])
        assert r.side == want["side"], (want["tf"], r.side, want["side"])
        assert r.text == want["text"], (want["tf"], r.text, want["text"])
        assert r.lowest_side == want["lowest"], (want["tf"], r.lowest_side)
        if want["factor"] == "inf":
            assert math.isinf(r.factor)
        else:
            assert abs(r.factor - float(want["factor"])) < 1e-9


def test_reading_maps_to_filter_contract():
    # Call/Put/Neutral → CALL/PUT/NO_TRADE (the §3.1 indicator contract:
    # Neutral is never emitted as a direction).
    rows = {"1m": (-444000.0, -2061000.0)}
    params = MtfRatioParams(
        timeframes=["1m"],
        rules=[MtfRule(name="r", on=True, out="Call", conditions=[MtfCondition(timeframe="1m")])],
    )
    assert eng.evaluate(rows, params).reading == "CALL"
    params.rules[0].out = "Put"
    assert eng.evaluate(rows, params).reading == "PUT"
    params.rules[0].out = "Neutral"
    assert eng.evaluate(rows, params).reading == "NO_TRADE"
    params.rules[0].on = False
    assert eng.evaluate(rows, params).reading == "NO_TRADE"


def test_rows_from_cumulative_series():
    # Cumulative-since-open arrays → trailing per-TF deltas; clamp to
    # since-open when the window exceeds available history.
    call = [0.0, 1.0, 1.5, 2.0, 2.2]   # 5 closed minutes
    put = [0.0, -0.5, -0.4, -1.0, -1.4]
    rows = eng.rows_from_cumulative_series(call, put, ["1m", "3m", "1h", "full_day"])

    def units(v: float) -> float:          # whole OI units, 1e-7 Cr
        return round(v * 1e7) / 1e7

    assert rows["1m"] == (units(2.2 - 2.0), units(-1.4 - -1.0))
    assert rows["3m"] == (units(2.2 - 1.0), units(-1.4 - -0.5))
    assert rows["1h"] == (2.2, -1.4), "window beyond history clamps to since-open"
    assert rows["full_day"] == (2.2, -1.4)
    # Rounding is UNIFORM across trailing, clamped and full_day rows —
    # previously full_day/clamped rows passed through unrounded, so a rule
    # comparing 1m against full_day saw two precisions (fixed 2026-08-18).
    # Since 2026-09-23 the unit is one whole OI unit, not 0.01 Cr.
    call2 = [0.12345678, 0.98765432]
    put2 = [-0.11111111, -0.55555555]
    rows2 = eng.rows_from_cumulative_series(call2, put2, ["1m", "1h", "full_day"])
    assert rows2["full_day"] == (0.9876543, -0.5555556)
    assert rows2["1h"] == (0.9876543, -0.5555556), "clamped row rounds identically"
    assert rows2["1m"] == (units(0.98765432 - 0.12345678), units(-0.55555555 - -0.11111111))


def test_small_one_minute_changes_are_not_zeroed():
    """2026-09-23 report: at 18 Sep 11:00 the Multi-TF page showed 1m
    ΔCE −5,980 / ΔPE +7,930 while Algo Config showed 0 / 0 → Neutral,
    because every row was rounded to 0.01 Cr (100,000 OI)."""
    call = [0.5507925, 0.5501945]          # Cr; −5,980 OI in the last minute
    put = [0.4963000, 0.4970930]           # Cr; +7,930 OI in the last minute
    rows = eng.rows_from_cumulative_series(call, put, ["1m"])
    c, p = rows["1m"]
    assert round(c * 1e7) == -5980 and round(p * 1e7) == 7930
    r = eng.ratio_reading("1m", c, p)
    assert r.side != "Neutral" and r.call_sign == "Negative" and r.put_sign == "Positive"


def test_a_true_zero_stays_zero():
    """Whole-unit rounding must not leave float residue that reads as a sign."""
    call = [0.1, 0.2, 0.3]
    rows = eng.rows_from_cumulative_series(call, [0.3, 0.3, 0.3], ["1m"])
    assert rows["1m"][1] == 0.0
    assert eng.ratio_reading("1m", *rows["1m"]).put_sign == "Zero"


def test_trace_explains_failures():
    rows = {"1m": (0.0, -500000.0)}
    params = MtfRatioParams(
        timeframes=["1m"],
        rules=[MtfRule(
            name="zero-sign", on=True, out="Call",
            conditions=[MtfCondition(timeframe="1m", call_sign="Negative", put_sign="Negative")],
        )],
    )
    res = eng.evaluate(rows, params)
    assert res.reading == "NO_TRADE"
    assert res.traces and not res.traces[0].matched
    assert any("Call sign failed" in c.reason for c in res.traces[0].checks), (
        res.traces[0].checks
    )


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
    print("all MTF Ratio golden tests passed")


if __name__ == "__main__":
    _run_all()
