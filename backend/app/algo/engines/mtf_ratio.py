"""MTF Ratio rule engine — the "Multi-TF" entry-filter indicator.

Faithful Python port of the Multi-Timeframe Ratio Analysis dashboard
(MTF_Ratio_Dashboard reference JS) per the MTF Ratio Master Guide.

Core concepts (guide §1):
- **Ratio Side** uses position MAGNITUDES only: the side with the smaller
  |OI Δ| is the Ratio Side, expressed "1 : N" with N = larger/smaller. It
  deliberately ignores signs — it answers "which side has less change?".
- **Lowest OI Side** is the separate, SIGNED comparison (the platform's
  classic dominant-side rule): whichever side has the lower signed Δ.
- **Signs** carry writer intent: Positive = writers adding (new supply),
  Negative = unwinding (buying back).

Rule semantics (guide §2–§4 + reference implementation quirks that MUST be
preserved):
- Rules evaluate top-to-bottom; the FIRST rule whose conditions ALL match
  wins and evaluation stops (§4). Disabled rules are skipped entirely.
- A rule with zero conditions can never match (reference: match requires
  ``conditions.length > 0``).
- Side filter: since 2026-09-13 a rule condition's side is matched against the
  **Lowest OI Side**, not the Ratio Side. The Ratio Side is still computed and
  still reported (it drives the "1 : N" text and the dashboards), it simply no
  longer decides a rule. A row whose Lowest OI Side is **Neutral passes any side
  filter**, carried over from the reference — but Neutral is much rarer under the
  signed rule, since it requires the two signed deltas to be exactly equal rather
  than equal in magnitude. Measured over four live sessions, the two definitions
  disagree on ~22% of readings, so this deliberately diverges from the reference
  JS and from the pre-2026-09-13 golden expectations.
- Operator: applied only when BOTH operator and threshold are set (non-Any).
  ``Above`` = factor >= target (wider than the target — capitulation);
  ``Below`` = factor <  target (tighter — consolidation/choke).
- Sign filters: Positive/Negative match exactly; a **zero Δ fails both**
  (its sign is "Zero", which equals neither).
- A condition naming a timeframe with no data row fails its whole rule.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Optional

from ..config_models import MtfRatioParams, MtfRule


@dataclass
class RatioReading:
    """One timeframe row's derived numbers."""
    timeframe: str
    call_delta: float
    put_delta: float
    side: Literal["Call", "Put", "Neutral"]
    factor: float                # >= 1; math.inf when one side is zero
    text: str                    # "1 : 2.50" / "2.50 : 1" / "1 : 1" / "1 : -"
    lowest_side: Literal["Call", "Put", "Neutral"]
    call_sign: Literal["Positive", "Negative", "Zero"]
    put_sign: Literal["Positive", "Negative", "Zero"]


@dataclass
class ConditionCheck:
    timeframe: str
    passed: bool
    reason: str                  # human-readable pass/fail explanation


@dataclass
class RuleTrace:
    name: str
    on: bool
    matched: bool
    out: str
    checks: list[ConditionCheck] = field(default_factory=list)


@dataclass
class MtfRatioResult:
    reading: Literal["CALL", "PUT", "NO_TRADE"]
    direction: Literal["Call", "Put", "Neutral"]
    matched_rule: Optional[str]
    rows: list[RatioReading] = field(default_factory=list)
    traces: list[RuleTrace] = field(default_factory=list)


def _sign(v: float) -> Literal["Positive", "Negative", "Zero"]:
    if v > 0:
        return "Positive"
    if v < 0:
        return "Negative"
    return "Zero"


def ratio_reading(timeframe: str, call_delta: float, put_delta: float) -> RatioReading:
    """Mirror of the reference ``ratioObj`` + ``lowestSide``."""
    c = abs(call_delta)
    p = abs(put_delta)
    if c == 0 and p == 0:
        side, factor, text = "Neutral", 1.0, "1 : 1"
    elif c == 0:
        side, factor, text = "Call", math.inf, "1 : -"
    elif p == 0:
        side, factor, text = "Put", math.inf, "- : 1"
    elif c == p:
        side, factor, text = "Neutral", 1.0, "1 : 1"
    elif c < p:
        side, factor, text = "Call", p / c, f"1 : {p / c:.2f}"
    else:
        side, factor, text = "Put", c / p, f"{c / p:.2f} : 1"

    if call_delta == put_delta:
        lowest = "Neutral"
    elif call_delta < put_delta:
        lowest = "Call"
    else:
        lowest = "Put"

    return RatioReading(
        timeframe=timeframe,
        call_delta=call_delta,
        put_delta=put_delta,
        side=side,  # type: ignore[arg-type]
        factor=factor,
        text=text,
        lowest_side=lowest,  # type: ignore[arg-type]
        call_sign=_sign(call_delta),
        put_sign=_sign(put_delta),
    )


def _parse_threshold(threshold: str) -> Optional[float]:
    """'1:2' → 2.0; 'Any' or junk → None (condition's operator is skipped —
    matches the reference, which only applies the operator when a parsable
    non-Any threshold is present)."""
    if threshold == "Any" or ":" not in threshold:
        return None
    try:
        return float(threshold.split(":", 1)[1])
    except ValueError:
        return None


def evaluate(
    rows: dict[str, tuple[float, float]],
    params: MtfRatioParams,
) -> MtfRatioResult:
    """rows: timeframe → (call Δ, put Δ). First 100% match wins, top-to-bottom."""
    readings = {
        tf: ratio_reading(tf, cd, pd) for tf, (cd, pd) in rows.items()
    }
    ordered_rows = [readings[tf] for tf in params.timeframes if tf in readings]
    # Rows outside the configured list still evaluate if a rule names them.
    for tf, r in readings.items():
        if tf not in params.timeframes:
            ordered_rows.append(r)

    traces: list[RuleTrace] = []
    direction: Literal["Call", "Put", "Neutral"] = "Neutral"
    matched_rule: Optional[str] = None

    for rule in params.rules:
        trace = RuleTrace(name=rule.name, on=rule.on, matched=False, out=rule.out)
        if not rule.on:
            traces.append(trace)
            continue

        match = len(rule.conditions) > 0
        for cond in rule.conditions:
            r = readings.get(cond.timeframe)
            if r is None:
                match = False
                trace.checks.append(
                    ConditionCheck(cond.timeframe, False, f"no data row for {cond.timeframe!r}")
                )
                break

            passed = True
            reasons: list[str] = []

            # Lowest-OI Side — the SIGNED comparison, the platform's dominant-side
            # rule (changed 2026-09-13; this used to filter on the magnitude-based
            # Ratio Side). Neutral still passes any side filter, kept from the
            # reference — but note Neutral is far rarer here: it needs the two
            # SIGNED deltas to be equal, not merely equal in magnitude.
            if (
                cond.side != "Any"
                and r.lowest_side != cond.side
                and r.lowest_side != "Neutral"
            ):
                passed = False
                reasons.append(
                    f"Lowest-OI Side failed (expected {cond.side}, got {r.lowest_side})"
                )

            # Operator + threshold — both must be non-Any to apply.
            target = _parse_threshold(cond.threshold)
            if target is not None and cond.operator != "Any":
                if cond.operator == "Above" and r.factor < target:
                    passed = False
                    reasons.append(f"Above failed (factor {r.factor:.2f} < {target})")
                elif cond.operator == "Below" and r.factor >= target:
                    passed = False
                    reasons.append(f"Below failed (factor {r.factor:.2f} >= {target})")

            # Sign filters — Zero fails both Positive and Negative.
            if cond.call_sign != "Any" and r.call_sign != cond.call_sign:
                passed = False
                reasons.append(f"Call sign failed (expected {cond.call_sign}, got {r.call_sign})")
            if cond.put_sign != "Any" and r.put_sign != cond.put_sign:
                passed = False
                reasons.append(f"Put sign failed (expected {cond.put_sign}, got {r.put_sign})")

            if not passed:
                match = False
            trace.checks.append(
                ConditionCheck(
                    cond.timeframe,
                    passed,
                    "; ".join(reasons) if reasons else
                    f"{cond.timeframe}: lowest-OI side {r.lowest_side}, ratio {r.text}, "
                    f"signs {r.call_sign[:3]}/{r.put_sign[:3]} — ok",
                )
            )

        if match:
            trace.matched = True
            traces.append(trace)
            direction = rule.out  # type: ignore[assignment]
            matched_rule = rule.name
            break
        traces.append(trace)

    reading: Literal["CALL", "PUT", "NO_TRADE"] = (
        "CALL" if direction == "Call" else "PUT" if direction == "Put" else "NO_TRADE"
    )
    return MtfRatioResult(
        reading=reading,
        direction=direction,
        matched_rule=matched_rule,
        rows=ordered_rows,
        traces=traces,
    )


def rows_from_cumulative_series(
    call_series: list[float],
    put_series: list[float],
    timeframes: list[str],
) -> dict[str, tuple[float, float]]:
    """Derive per-timeframe OI Δ rows from the cumulative-since-open series the
    OI Structure builder already produces (same basket, same units, same
    session-floor semantics — one data path for both engines).

    trailing Δ over m minutes = series[-1] − series[-1−m]; clamped to
    since-open when fewer than m closed minutes exist. full_day = series[-1].

    Every row is rounded to 2dp IDENTICALLY (fixed 2026-08-18: full_day and
    history-clamped rows used to pass through unrounded, so a rule comparing
    e.g. 1m against full_day saw two different precisions).
    """
    minutes_of = {
        "1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
        "30m": 30, "1h": 60, "2h": 120, "3h": 180,
    }

    def _r2(v: float) -> float:
        return round(v * 100) / 100

    out: dict[str, tuple[float, float]] = {}
    if not call_series or not put_series:
        return out
    n = min(len(call_series), len(put_series))
    for tf in timeframes:
        if tf == "full_day":
            out[tf] = (_r2(call_series[n - 1]), _r2(put_series[n - 1]))
            continue
        m = minutes_of.get(tf)
        if m is None:
            continue
        if m >= n:
            out[tf] = (_r2(call_series[n - 1]), _r2(put_series[n - 1]))
        else:
            out[tf] = (
                _r2(call_series[n - 1] - call_series[n - 1 - m]),
                _r2(put_series[n - 1] - put_series[n - 1 - m]),
            )
    return out


# ── serialisation (one shape for the dashboard endpoint AND the decision trace)
_TRACE_CAP = 40


def reading_to_dict(r: "RatioReading") -> dict:
    return {
        "timeframe": r.timeframe,
        "call_delta_cr": r.call_delta,
        "put_delta_cr": r.put_delta,
        "side": r.side,
        "factor": None if r.factor == float("inf") else round(r.factor * 100) / 100,
        "text": r.text,
        "lowest_side": r.lowest_side,
        "call_sign": r.call_sign,
        "put_sign": r.put_sign,
    }


def result_to_dict(result: "MtfRatioResult") -> dict:
    return {
        "reading": result.reading,
        "direction": result.direction,
        "matched_rule": result.matched_rule,
        "rows": [reading_to_dict(r) for r in result.rows],
        "traces": [
            {
                "name": t.name,
                "on": t.on,
                "matched": t.matched,
                "out": t.out,
                "checks": [
                    {"timeframe": c.timeframe, "passed": c.passed, "reason": c.reason}
                    for c in t.checks[:_TRACE_CAP]
                ],
            }
            for t in result.traces[:_TRACE_CAP]
        ],
    }
