"""OI Structure Engine — the "OI Change" entry-filter indicator.

Faithful Python port of ``oi_smc_engine.html`` per its Developer Execution
Specification (oi_structure_engine_spec.pdf). The engine turns two raw
1-minute Open-Interest-change series (Call side and Put side, in Crores,
cumulative since market open) into one signal: CALL, PUT or NO_TRADE, plus a
0–100 confidence score and the exact Algo Payload a downstream execution
system consumes.

Deliberate scope (spec §1): HH/HL/LH/LL swing structure + a swing-bounded
range breakout on the 1-minute streams, plus ONE fixed red-candle rule on the
derived 5-minute streams. There is NO BOS, NO CHoCH and no N-bar range system
— all three were removed on explicit instruction; re-adding them is a new
feature, not a fix.

Port-fidelity notes (each anchors to the reference JS):
- findSwings consolidation keeps the MORE-extreme same-type swing using >= / <=
  (ties replace), and the minMove noise filter applies only between
  ALTERNATING-type swings — dropped entirely, never merged.
- classifyStructure checks EQH/EQL BEFORE higher/lower (|diff| <= tolerance):
  a near-tie is a liquidity pool, not fresh structure. First-of-kind swings
  are labelled H/L and never score.
- getZone keeps the LAST high and LAST low seen in the classified list and
  guards inversion with hi=max/lo=min.
- detectRangeBreakout reads only the series' final value with a STRICT
  inequality beyond the buffered edge; no breakout history is kept.
- checkRedCandle looks at candles[-2] — the most recently FULLY CLOSED 5m
  candle; the forming candle is skipped by construction.
- computeScore tallies EVERY confirmed HH/HL/LH/LL in the window (a
  session-cumulative tally, not just the latest event), routes through the
  Interpretation Matrix, adds breakout (+ liquidity-sweep bonus within the
  FIXED 0.05 Cr tolerance of a marked EQH/EQL on the same chart), adds the
  fixed 5m red-candle weight per chart, then the agreement bonus once to the
  leader only when BOTH sides scored.
- Ties favour Call (>= in the leader comparison) — spec §10 says a refactor
  must preserve this exact tie-break.
- Payload ``confidence`` is capped at 100 cosmetically; the threshold gate
  compares the UNCAPPED leader points.

Closed-candle discipline (spec §2): the caller must pass only fully closed
1-minute values — the swing detector has its own confirmation delay, but the
breakout and red-candle checks trust the caller. The series builder enforces
this; the engine does not re-check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from ..config_models import OIStructureParams

SwingType = Literal["high", "low"]


def _js_round(x: float) -> float:
    """JS ``Math.round`` — half rounds UP (toward +∞), unlike Python's
    banker's rounding. Matters for golden parity with fractional weights."""
    import math

    return math.floor(x + 0.5)

# The liquidity-sweep tolerance is a fixed constant in the reference build
# (spec §8.2) — deliberately NOT a config field until the spec promotes it.
SWEEP_TOLERANCE_CR = 0.05

_EVENT_IDS = ("HH", "HL", "LH", "LL", "BREAKOUT_UP", "BREAKOUT_DOWN")


@dataclass
class Swing:
    index: int
    value: float
    type: SwingType
    label: str = ""          # H/L (first of kind), HH/HL/LH/LL, EQH/EQL


@dataclass
class Candle:
    o: float
    h: float
    l: float
    c: float


@dataclass
class Zone:
    hi: float
    lo: float


@dataclass
class Breakout:
    direction: Optional[Literal["up", "down"]]
    value: Optional[float] = None


@dataclass
class Contribution:
    label: str
    side: str                # Call / Put / Ignore
    points: float


@dataclass
class OIStructureResult:
    signal: Literal["CALL", "PUT", "NO_TRADE"]
    confidence: int          # min(100, round(leader)) — cosmetic cap
    raw_direction: Literal["CALL", "PUT"]
    call_pts: float
    put_pts: float
    call_swings: list[Swing] = field(default_factory=list)
    put_swings: list[Swing] = field(default_factory=list)
    call_zone: Optional[Zone] = None
    put_zone: Optional[Zone] = None
    call_breakout: Breakout = field(default_factory=lambda: Breakout(None))
    put_breakout: Breakout = field(default_factory=lambda: Breakout(None))
    call_red_5m: bool = False
    put_red_5m: bool = False
    contributions: list[Contribution] = field(default_factory=list)
    # F4 — how many of the swings above actually scored at this bar. Equal to
    # len(call_swings)/len(put_swings) when the lookback is 0 (whole session).
    scored_call_swings: int = 0
    scored_put_swings: int = 0

    def algo_payload(self, *, timestamp: str, engine_1m_on: bool, engine_5m_on: bool) -> dict:
        """The exact JSON object of spec §11 — the downstream contract.
        ``engineActive`` is part of the signal's identity: a PUT with both
        engines on is different evidence from a PUT with one alone."""
        return {
            "timestamp": timestamp,
            "signal": self.signal,
            "confidence": self.confidence,
            "rawDirection": self.raw_direction,
            "engineActive": {"oneMinute": engine_1m_on, "fiveMinute": engine_5m_on},
            "zones": {
                "call1m": {"hi": self.call_zone.hi, "lo": self.call_zone.lo}
                if self.call_zone else None,
                "put1m": {"hi": self.put_zone.hi, "lo": self.put_zone.lo}
                if self.put_zone else None,
            },
            "breakouts": {
                "call1m": self.call_breakout.direction,
                "put1m": self.put_breakout.direction,
            },
            "redCandle5m": {"call": self.call_red_5m, "put": self.put_red_5m},
            "scores": {"call": self.call_pts, "put": self.put_pts},
        }


# ══════════════════════════════════════════════════════════════════════════
# Detection primitives (spec §3–§6)
# ══════════════════════════════════════════════════════════════════════════

def to_candles(series_1m: list[float], size: int = 5) -> list[Candle]:
    """Fold consecutive groups of ``size`` one-minute values into OHLC candles
    (spec §2): open = first, high = max, low = min, close = last. A trailing
    partial group still forms a candle — it is the 'still forming' one.

    Signal Console F5 §5.8 made the group size a setting; it was hard-coded 5."""
    step = size if size and size > 1 else 5
    out: list[Candle] = []
    for i in range(0, len(series_1m), step):
        chunk = series_1m[i : i + step]
        if not chunk:
            break
        out.append(Candle(o=chunk[0], h=max(chunk), l=min(chunk), c=chunk[-1]))
    return out


def to_5m(series_1m: list[float]) -> list[Candle]:
    """Back-compatible alias for the original fixed 5-bar fold."""
    return to_candles(series_1m, 5)


def find_swings(series: list[float], left: int, right: int, min_move: float) -> list[Swing]:
    """Fractal swing detection + consolidation + the minMove noise filter
    (spec §3). Confirmation delay is inherent: a bar qualifies only once
    ``right`` bars after it exist, so nothing here repaints."""
    raw: list[Swing] = []
    for i in range(left, len(series) - right):
        window = series[i - left : i + right + 1]
        v = series[i]
        if v == max(window):
            raw.append(Swing(index=i, value=v, type="high"))
        elif v == min(window):
            raw.append(Swing(index=i, value=v, type="low"))

    filtered: list[Swing] = []
    for s in raw:
        if not filtered:
            filtered.append(s)
            continue
        prev = filtered[-1]
        if prev.type == s.type:
            # Same type back-to-back → keep the single most extreme one
            # (>= / <= : a tie replaces, matching the reference exactly).
            if (s.type == "high" and s.value >= prev.value) or (
                s.type == "low" and s.value <= prev.value
            ):
                filtered[-1] = s
            continue
        # Alternating type: drop entirely when within minMove of the last
        # accepted swing — the system's ONLY noise filter.
        if abs(s.value - prev.value) < min_move:
            continue
        filtered.append(s)
    return filtered


def classify_structure(swings: list[Swing], eq_tolerance: float) -> list[Swing]:
    """Label every accepted swing vs the previous swing of the SAME type
    (spec §4). The equal-high/low check runs FIRST: within tolerance the label
    is EQH/EQL regardless of which is technically larger."""
    out: list[Swing] = []
    for i, s in enumerate(swings):
        label = "H" if s.type == "high" else "L"
        prev_same: Optional[Swing] = None
        for j in range(i - 1, -1, -1):
            if swings[j].type == s.type:
                prev_same = swings[j]
                break
        if prev_same is not None:
            diff = s.value - prev_same.value
            if abs(diff) <= eq_tolerance:
                label = "EQH" if s.type == "high" else "EQL"
            elif s.type == "high":
                label = "HH" if diff > 0 else "LH"
            else:
                label = "HL" if diff > 0 else "LL"
        out.append(Swing(index=s.index, value=s.value, type=s.type, label=label))
    return out


def get_zone(structured: list[Swing]) -> Optional[Zone]:
    """Zone = [value of the most recent confirmed swing low, most recent
    confirmed swing high] — nothing else defines it (spec §5)."""
    last_high: Optional[Swing] = None
    last_low: Optional[Swing] = None
    for s in structured:
        if s.type == "high":
            last_high = s
        else:
            last_low = s
    if last_high is None or last_low is None:
        return None
    return Zone(
        hi=max(last_high.value, last_low.value),
        lo=min(last_high.value, last_low.value),
    )


def detect_range_breakout(
    series: list[float], zone: Optional[Zone], buffer: float
) -> Breakout:
    """Strictly-beyond-the-buffered-edge check on the series' LAST value
    (spec §5). Stateless: re-running next candle re-decides from scratch."""
    if zone is None or not series:
        return Breakout(None)
    last = series[-1]
    if last > zone.hi + buffer:
        return Breakout("up", last)
    if last < zone.lo - buffer:
        return Breakout("down", last)
    return Breakout(None)


def check_red_candle(candles: list[Candle]) -> bool:
    """PRE-2026-09-19 rule, kept only so the original behaviour stays testable:
    always read index -2 and assume -1 is still forming. Superseded by
    ``red_candle`` — see the divergence note there. Not used by ``evaluate``."""
    if len(candles) < 2:
        return False
    last_closed = candles[-2]
    return last_closed.c < last_closed.o


def red_candle(series_1m: list[float], size: int = 5, read: str = "closed") -> bool:
    """The entire candle rule (spec §6; Signal Console F5 §5.3–§5.6): is the
    candle we are supposed to read red? Green does nothing.

    ``forming`` reads the candle still being built — its close is the latest
    value, so the answer can flip while the candle lives (F5 §5.6).
    ``closed`` reads the most recent candle whose period has COMPLETELY
    finished.

    Deliberate divergence from the reference JavaScript (and from this engine
    before 2026-09-19), both of which always read ``candles[-2]``. That is
    right only while the final group is partial; the moment a group completes,
    ``[-2]`` skips a genuinely finished candle. At size 5 it was stale one
    minute in five; at size 15 it would be stale one minute in fifteen."""
    candles = to_candles(series_1m, size)
    if not candles:
        return False
    step = size if size and size > 1 else 5
    if read == "forming":
        chosen = candles[-1]
    else:
        complete = len(series_1m) // step
        if complete == 0:
            return False
        chosen = candles[complete - 1]
    return chosen.c < chosen.o


def swings_in_lookback(swings: list[Swing], series_len: int, lookback: int) -> list[Swing]:
    """Signal Console F4 §4.3–§4.5 — the swings allowed to CONTRIBUTE POINTS at
    the current bar. ``lookback`` 0 means the whole session (original
    behaviour). Otherwise the window floor is ``current_bar − lookback`` and a
    swing sitting exactly ON the floor is included (§4.4).

    Callers must keep passing the UNFILTERED list to ``get_zone`` and
    ``detect_range_breakout``: a short lookback must never erase a range zone
    formed earlier in the session (§4.8)."""
    if lookback <= 0 or not swings:
        return swings
    floor = (series_len - 1) - lookback
    return [s for s in swings if s.index >= floor]


# ══════════════════════════════════════════════════════════════════════════
# Scoring + gating (spec §7–§10)
# ══════════════════════════════════════════════════════════════════════════

def evaluate(
    call_series_1m: list[float],
    put_series_1m: list[float],
    params: OIStructureParams,
) -> OIStructureResult:
    """One full recompute: detection on both 1m streams, derived 5m rule,
    matrix routing, scoring, confidence gate. Pure — same inputs, same output.
    """
    p = params
    call_swings = classify_structure(
        find_swings(call_series_1m, p.left_bars, p.right_bars, p.min_swing_move_cr),
        p.eq_tolerance_cr,
    )
    put_swings = classify_structure(
        find_swings(put_series_1m, p.left_bars, p.right_bars, p.min_swing_move_cr),
        p.eq_tolerance_cr,
    )
    # §4.8 / F4: zones and breakouts ALWAYS read the complete structure through
    # the current bar. The lookback below filters scoring inputs only.
    call_zone = get_zone(call_swings)
    put_zone = get_zone(put_swings)
    call_break = detect_range_breakout(call_series_1m, call_zone, p.breakout_buffer_cr)
    put_break = detect_range_breakout(put_series_1m, put_zone, p.breakout_buffer_cr)
    call_red = red_candle(call_series_1m, p.candle_size_bars, p.candle_read)
    put_red = red_candle(put_series_1m, p.candle_size_bars, p.candle_read)

    # F4 §4.3 — the swings that may contribute points at this bar.
    scored_call = swings_in_lookback(call_swings, len(call_series_1m), p.swing_scoring_lookback)
    scored_put = swings_in_lookback(put_swings, len(put_series_1m), p.swing_scoring_lookback)

    call_pts = 0.0
    put_pts = 0.0
    contrib: list[Contribution] = []

    matrix = {"call": p.matrix.call, "put": p.matrix.put}
    # The sweep bonus reads EQ levels from the SCORED list, matching the
    # reference (computeScore derives eqCall/eqPut from the swings it is given).
    eq_call = [s.value for s in scored_call if s.label in ("EQH", "EQL")]
    eq_put = [s.value for s in scored_put if s.label in ("EQH", "EQL")]

    def route_swings(swings: list[Swing], chart: str) -> None:
        nonlocal call_pts, put_pts
        for s in swings:
            # EQH/EQL never score directly (they feed the sweep bonus);
            # first-of-kind H/L are unlabelled structure and never score.
            if s.label in ("EQH", "EQL", "H", "L"):
                continue
            side = matrix[chart].get(s.label, "Ignore")
            if side == "Call":
                call_pts += p.swing_weight
            elif side == "Put":
                put_pts += p.swing_weight
            contrib.append(
                Contribution(
                    label=f"{'Call' if chart == 'call' else 'Put'} 1m {s.label}",
                    side=side,
                    points=p.swing_weight,
                )
            )

    def route_breakout(bo: Breakout, chart: str, eq_levels: list[float]) -> None:
        nonlocal call_pts, put_pts
        if bo.direction is None or bo.value is None:
            return
        pts = p.breakout_weight
        swept = any(abs(v - bo.value) < SWEEP_TOLERANCE_CR for v in eq_levels)
        if swept:
            pts += p.sweep_weight
        ev_id = "BREAKOUT_UP" if bo.direction == "up" else "BREAKOUT_DOWN"
        side = matrix[chart].get(ev_id, "Ignore")
        if side == "Call":
            call_pts += pts
        elif side == "Put":
            put_pts += pts
        contrib.append(
            Contribution(
                label=(
                    f"{'Call' if chart == 'call' else 'Put'} Range Breakout "
                    f"{bo.direction.upper()}{' (swept EQ level)' if swept else ''}"
                ),
                side=side,
                points=pts,
            )
        )

    if p.engine_1m_on:
        route_swings(scored_call, "call")
        route_swings(scored_put, "put")
        route_breakout(call_break, "call", eq_call)
        route_breakout(put_break, "put", eq_put)
    else:
        contrib.append(
            Contribution(
                label="1-Minute Structure Engine is OFF — HH/HL/LH/LL and Range Breakout excluded",
                side="Ignore",
                points=0.0,
            )
        )

    if p.engine_5m_on:
        # The fixed rule bypasses the matrix entirely: a red candle on a chart
        # routes to that same chart's side, always (spec §6).
        if call_red:
            call_pts += p.red_candle_weight
            contrib.append(
                Contribution("Call 5m last candle (Red, fixed rule)", "Call", p.red_candle_weight)
            )
        if put_red:
            put_pts += p.red_candle_weight
            contrib.append(
                Contribution("Put 5m last candle (Red, fixed rule)", "Put", p.red_candle_weight)
            )
    else:
        contrib.append(
            Contribution(
                label="5-Minute Red Candle Rule is OFF — excluded regardless of candle colour",
                side="Ignore",
                points=0.0,
            )
        )

    # Agreement bonus: only when BOTH sides accumulated something, added once
    # to whichever side currently leads (Call on a tie — >=).
    if call_pts > 0 and put_pts > 0:
        if call_pts >= put_pts:
            call_pts += p.agreement_bonus
            contrib.append(Contribution("Call+Put agreement bonus", "Call", p.agreement_bonus))
        else:
            put_pts += p.agreement_bonus
            contrib.append(Contribution("Call+Put agreement bonus", "Put", p.agreement_bonus))

    # Reference parity: the tallies are rounded to one decimal FIRST and the
    # gate compares those rounded values (the JS rounds inside computeScore and
    # gates on its return). Ties favour Call (>=); the threshold sees the
    # uncapped leader — only the payload's confidence field is capped at 100.
    call_pts_r = _js_round(call_pts * 10) / 10
    put_pts_r = _js_round(put_pts * 10) / 10
    leader_side: Literal["CALL", "PUT"] = "CALL" if call_pts_r >= put_pts_r else "PUT"
    leader_pts = max(call_pts_r, put_pts_r)
    gated = leader_pts < p.confidence_threshold
    signal: Literal["CALL", "PUT", "NO_TRADE"] = "NO_TRADE" if gated else leader_side

    return OIStructureResult(
        signal=signal,
        confidence=int(min(100, _js_round(leader_pts))),
        raw_direction=leader_side,
        call_pts=call_pts_r,
        put_pts=put_pts_r,
        call_swings=call_swings,
        put_swings=put_swings,
        call_zone=call_zone,
        put_zone=put_zone,
        call_breakout=call_break,
        put_breakout=put_break,
        call_red_5m=call_red,
        put_red_5m=put_red,
        contributions=contrib,
        scored_call_swings=len(scored_call),
        scored_put_swings=len(scored_put),
    )


# ── serialisation (one shape for the dashboard endpoint AND the decision trace)
_TRACE_CAP = 40


def result_to_dict(result: "OIStructureResult", *, last_call_cr: Optional[float] = None,
                   last_put_cr: Optional[float] = None) -> dict:
    """JSON-safe, deterministic (fixed key order) summary of an evaluation.
    Lists are capped so a per-minute trace row stays small."""
    return {
        "signal": result.signal,
        "confidence": result.confidence,
        "raw_direction": result.raw_direction,
        "call_pts": result.call_pts,
        "put_pts": result.put_pts,
        "call_breakout": result.call_breakout.direction,
        "put_breakout": result.put_breakout.direction,
        "call_red_5m": result.call_red_5m,
        "put_red_5m": result.put_red_5m,
        "call_zone": [result.call_zone.lo, result.call_zone.hi] if result.call_zone else None,
        "put_zone": [result.put_zone.lo, result.put_zone.hi] if result.put_zone else None,
        "call_swings": len(result.call_swings),
        "put_swings": len(result.put_swings),
        # F4 — visible proof the lookback filter is doing something; equals the
        # totals above whenever the lookback is 0.
        "scored_call_swings": result.scored_call_swings,
        "scored_put_swings": result.scored_put_swings,
        "last_call_cr": last_call_cr,
        "last_put_cr": last_put_cr,
        "contributions": [
            {"label": c.label, "side": c.side, "points": c.points}
            for c in result.contributions[:_TRACE_CAP]
        ],
    }
