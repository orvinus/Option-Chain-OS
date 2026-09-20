"""Master Quantitative Action Engine — the "Ratio" entry-filter indicator.

Faithful Python port of the reference implementation (the full HTML/JS source
preserved in ``ratio html code.docx``) per its Product Documentation Sheet.

Inputs are two 1-minute lines that move inversely by construction:
- **Green = the PCR line** (put/call OI ratio series)
- **Yellow = the Ratio line** (call/put OI ratio series)
— exactly the two series the platform's Ratio tab already renders in those
colours.

Five models, each with a kill switch, evaluated on BOTH lines (the Yellow
line scores with inverse interpretation throughout) plus a direct crossover:

1. Kinetic Velocity  — RoC over ``velocity_period``; |v| > threshold = surge.
2. Order Blocks      — impulse moves between consecutive swings create
   DEMAND/SUPPLY zones (±``ob_zone_width``); price breaking through kills a
   zone; the LIVE price touching an active zone scores.
3. SMC Structure     — swing HH/HL bullish, LH/LL bearish; ONLY the latest
   swing scores (deliberately different from the OI Structure Engine's
   cumulative tally).
4. Macro Trend Rider — ratcheting trail; contributes exactly ±1 in BOTH risk
   modes (a confirmation layer, never a dominant factor).
5. Crossover         — Green above Yellow = bullish, below = bearish.

Risk modes rewire the weights (PDS §4): **aggressive** = velocity ×3, order
blocks ×3, threshold 3 (early entry); **conservative** = SMC ×3, crossover
×3, threshold 6 (confirmation-heavy). Modes are mutually exclusive and never
both off — the config model's Literal enforces that statically.

Port-fidelity notes (each anchors to the reference JS, which is ground truth
where the PDS prose disagrees):
- ``extractSwings`` compares each bar against the ``bars`` bars BEFORE it
  only (the PDS says "both sides"; the code does not — port the code).
  Same-type consolidation keeps the more extreme with STRICT >/<; the
  alternating minMove filter KEEPS a swing only when the gap >= minMove.
- Order-block invalidation scans strictly AFTER the origin bar; the touch
  test is inclusive on both zone edges.
- TotalScore = ScoreGreen + ScoreYellow + ScoreCrossover; signal CALL when
  total >= threshold, PUT when total <= −threshold, else NO TRADE.
- The execution history replays ``evaluateState`` over growing slices
  starting at bar 30 and records every signal TRANSITION (the first
  evaluated state always records, ``lastSig`` starts as a sentinel).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Optional

from ..config_models import MqaeParams

Signal = Literal["CALL", "PUT", "NO_TRADE"]

# The reference starts its history replay at bar 30 — enough bars for the
# default lookbacks to produce meaningful state.
HISTORY_START_BAR = 30


@dataclass
class MqaeSwing:
    index: int
    value: float
    type: Literal["high", "low"]
    label: str          # H/L (first of kind), HH/HL/LH/LL


@dataclass
class OrderBlock:
    type: Literal["DEMAND", "SUPPLY"]
    top: float
    bot: float
    origin_index: int
    active: bool


@dataclass
class RiderPoint:
    val: float
    trend: int          # 1 up / -1 down


@dataclass
class ModelLog:
    model: str
    text: str
    points: float       # signed: + toward CALL, − toward PUT


@dataclass
class MqaeResult:
    signal: Signal
    total: float
    score_green: float
    score_yellow: float
    score_cross: float
    logs_green: list[ModelLog] = field(default_factory=list)
    logs_yellow: list[ModelLog] = field(default_factory=list)
    logs_cross: list[ModelLog] = field(default_factory=list)
    # Overlays for the dashboard (computed on the full slice):
    green_swings: list[MqaeSwing] = field(default_factory=list)
    yellow_swings: list[MqaeSwing] = field(default_factory=list)
    green_blocks: list[OrderBlock] = field(default_factory=list)
    yellow_blocks: list[OrderBlock] = field(default_factory=list)
    green_rider: list[RiderPoint] = field(default_factory=list)
    yellow_rider: list[RiderPoint] = field(default_factory=list)
    green_velocity: list[float] = field(default_factory=list)
    yellow_velocity: list[float] = field(default_factory=list)


@dataclass
class HistoryTransition:
    bar: int            # slice length at which the signal changed
    signal: Signal
    total: float


# ══════════════════════════════════════════════════════════════════════════
# Mathematical models (verbatim ports)
# ══════════════════════════════════════════════════════════════════════════

def calc_velocity(prices: list[float], period: int) -> list[float]:
    """First difference over ``period`` bars; 0 while history is insufficient."""
    out: list[float] = []
    for i in range(len(prices)):
        if i < period:
            out.append(0.0)
        else:
            out.append(prices[i] - prices[i - period])
    return out


def extract_swings(prices: list[float], bars: int, min_move: float) -> list[MqaeSwing]:
    """Reference ``extractSwings``: a bar is a swing high when STRICTLY greater
    than the ``bars`` bars before it (left-side only — the code, not the PDS
    prose, is authoritative); mirror for lows. Same-type consolidation keeps
    the strictly-more-extreme; an alternating swing survives only when at
    least ``min_move`` away from the last kept one."""
    raw: list[MqaeSwing] = []
    for i in range(bars, len(prices) - bars):
        is_h = True
        is_l = True
        for j in range(1, bars + 1):
            if prices[i] <= prices[i - j]:
                is_h = False
            if prices[i] >= prices[i - j]:
                is_l = False
        if is_h:
            raw.append(MqaeSwing(index=i, value=prices[i], type="high", label=""))
        if is_l:
            raw.append(MqaeSwing(index=i, value=prices[i], type="low", label=""))

    filtered: list[MqaeSwing] = []
    for s in raw:
        if not filtered:
            filtered.append(s)
            continue
        prev = filtered[-1]
        if prev.type == s.type:
            if (s.type == "high" and s.value > prev.value) or (
                s.type == "low" and s.value < prev.value
            ):
                filtered[-1] = s
            continue
        if abs(s.value - prev.value) >= min_move:
            filtered.append(s)

    for i, s in enumerate(filtered):
        prev_same: Optional[MqaeSwing] = None
        for j in range(i - 1, -1, -1):
            if filtered[j].type == s.type:
                prev_same = filtered[j]
                break
        if prev_same is not None:
            if s.type == "high":
                s.label = "HH" if s.value > prev_same.value else "LH"
            else:
                s.label = "HL" if s.value > prev_same.value else "LL"
        else:
            s.label = "H" if s.type == "high" else "L"
    return filtered


def find_order_blocks(
    prices: list[float], swings: list[MqaeSwing], impulse: float, width: float
) -> list[OrderBlock]:
    """Consecutive swing pairs whose distance clears ``impulse`` create a
    zone at the FIRST swing; price subsequently breaking through the zone
    boundary invalidates it. Only active blocks are returned/scored."""
    blocks: list[OrderBlock] = []
    for i in range(1, len(swings)):
        s1, s2 = swings[i - 1], swings[i]
        if abs(s2.value - s1.value) >= impulse:
            if s1.type == "low" and s2.value > s1.value:
                blocks.append(OrderBlock("DEMAND", s1.value + width, s1.value, s1.index, True))
            elif s1.type == "high" and s2.value < s1.value:
                blocks.append(OrderBlock("SUPPLY", s1.value, s1.value - width, s1.index, True))
    for ob in blocks:
        for j in range(ob.origin_index + 1, len(prices)):
            if ob.type == "DEMAND" and prices[j] < ob.bot:
                ob.active = False
            if ob.type == "SUPPLY" and prices[j] > ob.top:
                ob.active = False
    return [ob for ob in blocks if ob.active]


def calculate_trend_rider(prices: list[float], period: int, buffer: float) -> list[RiderPoint]:
    """Ratcheting trailing state machine. The first ``period`` rows carry
    trend=1 and val=price−buffer WITHOUT advancing the internal trail —
    exactly like the reference."""
    out: list[RiderPoint] = []
    if not prices:
        return out
    trend = 1
    trail = prices[0]
    for i in range(len(prices)):
        if i < period:
            out.append(RiderPoint(val=prices[i] - buffer, trend=1))
            continue
        current = prices[i]
        window = prices[i - period : i]
        highest = max(window)
        lowest = min(window)
        if trend == 1:
            new_trail = lowest - buffer
            if new_trail > trail:
                trail = new_trail
            if current < trail:
                trend = -1
                trail = highest + buffer
        else:
            new_trail = highest + buffer
            if new_trail < trail:
                trail = new_trail
            if current > trail:
                trend = 1
                trail = lowest - buffer
        out.append(RiderPoint(val=trail, trend=trend))
    return out


# ══════════════════════════════════════════════════════════════════════════
# Synthesis (reference ``evaluateState``)
# ══════════════════════════════════════════════════════════════════════════

def evaluate(green: list[float], yellow: list[float], params: MqaeParams) -> MqaeResult:
    """One full state evaluation on the given slices (closed candles only —
    the series builder enforces the discipline)."""
    p = params
    live_g = green[-1] if green else 0.0
    live_y = yellow[-1] if yellow else 0.0

    vel_g = calc_velocity(green, p.velocity_period)
    vel_y = calc_velocity(yellow, p.velocity_period)
    swings_g = extract_swings(green, p.smc_swing_bars, p.smc_min_move)
    swings_y = extract_swings(yellow, p.smc_swing_bars, p.smc_min_move)
    obs_g = find_order_blocks(green, swings_g, p.ob_impulse_trigger, p.ob_zone_width)
    obs_y = find_order_blocks(yellow, swings_y, p.ob_impulse_trigger, p.ob_zone_width)
    rider_g = calculate_trend_rider(green, p.trail_period, p.trail_buffer)
    rider_y = calculate_trend_rider(yellow, p.trail_period, p.trail_buffer)

    aggressive = p.risk_mode == "aggressive"
    w_vel = 3.0 if aggressive else 1.0
    w_ob = 3.0 if aggressive else 1.0
    w_smc = 1.0 if aggressive else 3.0
    w_cross = 1.0 if aggressive else 3.0

    score_g = 0.0
    score_y = 0.0
    score_cross = 0.0
    logs_g: list[ModelLog] = []
    logs_y: list[ModelLog] = []
    logs_c: list[ModelLog] = []

    # 1. GREEN evaluation (direct interpretation).
    if p.velocity_on and vel_g:
        v = vel_g[-1]
        if v > p.velocity_surge_threshold:
            score_g += w_vel
            logs_g.append(ModelLog("velocity", "Velocity UP Surge", +w_vel))
        elif v < -p.velocity_surge_threshold:
            score_g -= w_vel
            logs_g.append(ModelLog("velocity", "Velocity DOWN Surge", -w_vel))
    if p.order_blocks_on:
        for ob in obs_g:
            if ob.bot <= live_g <= ob.top:
                if ob.type == "DEMAND":
                    score_g += w_ob
                    logs_g.append(ModelLog("order_blocks", "Touching DEMAND Block", +w_ob))
                else:
                    score_g -= w_ob
                    logs_g.append(ModelLog("order_blocks", "Touching SUPPLY Block", -w_ob))
    if p.smc_on and swings_g:
        s = swings_g[-1]
        if s.label in ("HH", "HL"):
            score_g += w_smc
            logs_g.append(ModelLog("smc", f"SMC Structure ({s.label})", +w_smc))
        elif s.label in ("LH", "LL"):
            score_g -= w_smc
            logs_g.append(ModelLog("smc", f"SMC Structure ({s.label})", -w_smc))
    if p.trend_rider_on and rider_g:
        if rider_g[-1].trend == 1:
            score_g += 1.0
            logs_g.append(ModelLog("trend_rider", "Macro Rider Intact", +1.0))
        else:
            score_g -= 1.0
            logs_g.append(ModelLog("trend_rider", "Macro Rider Broken", -1.0))

    # 2. YELLOW evaluation (INVERSE interpretation throughout).
    if p.velocity_on and vel_y:
        v = vel_y[-1]
        if v > p.velocity_surge_threshold:
            score_y -= w_vel
            logs_y.append(ModelLog("velocity", "Velocity UP Surge", -w_vel))
        elif v < -p.velocity_surge_threshold:
            score_y += w_vel
            logs_y.append(ModelLog("velocity", "Velocity DOWN Surge", +w_vel))
    if p.order_blocks_on:
        for ob in obs_y:
            if ob.bot <= live_y <= ob.top:
                if ob.type == "DEMAND":
                    score_y -= w_ob
                    logs_y.append(ModelLog("order_blocks", "Touching DEMAND Block", -w_ob))
                else:
                    score_y += w_ob
                    logs_y.append(ModelLog("order_blocks", "Touching SUPPLY Block", +w_ob))
    if p.smc_on and swings_y:
        s = swings_y[-1]
        if s.label in ("HH", "HL"):
            score_y -= w_smc
            logs_y.append(ModelLog("smc", f"SMC Structure ({s.label})", -w_smc))
        elif s.label in ("LH", "LL"):
            score_y += w_smc
            logs_y.append(ModelLog("smc", f"SMC Structure ({s.label})", +w_smc))
    if p.trend_rider_on and rider_y:
        if rider_y[-1].trend == 1:
            score_y -= 1.0
            logs_y.append(ModelLog("trend_rider", "Macro Rider Intact", -1.0))
        else:
            score_y += 1.0
            logs_y.append(ModelLog("trend_rider", "Macro Rider Broken", +1.0))

    # 3. CROSSOVER — direct comparison of the two lines' current values.
    if p.crossover_on:
        if live_g > live_y:
            score_cross += w_cross
            logs_c.append(ModelLog("crossover", "Green > Yellow", +w_cross))
        elif live_y > live_g:
            score_cross -= w_cross
            logs_c.append(ModelLog("crossover", "Yellow > Green", -w_cross))

    total = score_g + score_y + score_cross
    if total >= p.master_threshold:
        signal: Signal = "CALL"
    elif total <= -p.master_threshold:
        signal = "PUT"
    else:
        signal = "NO_TRADE"

    return MqaeResult(
        signal=signal,
        total=total,
        score_green=score_g,
        score_yellow=score_y,
        score_cross=score_cross,
        logs_green=logs_g,
        logs_yellow=logs_y,
        logs_cross=logs_c,
        green_swings=swings_g,
        yellow_swings=swings_y,
        green_blocks=obs_g,
        yellow_blocks=obs_y,
        green_rider=rider_g,
        yellow_rider=rider_y,
        green_velocity=vel_g,
        yellow_velocity=vel_y,
    )


def history_transitions(
    green: list[float], yellow: list[float], params: MqaeParams
) -> list[HistoryTransition]:
    """Replay ``evaluate`` over growing slices (bars 30..N, slice EXCLUDES the
    end index — JS ``slice(0, i)`` semantics) and record every signal
    transition. The first evaluated state always records (the reference's
    ``lastSig`` starts as a sentinel that matches nothing)."""
    n = min(len(green), len(yellow))
    out: list[HistoryTransition] = []
    last: Optional[Signal] = None
    for i in range(HISTORY_START_BAR, n + 1):
        state = evaluate(green[:i], yellow[:i], params)
        if state.signal != last:
            out.append(HistoryTransition(bar=i, signal=state.signal, total=state.total))
            last = state.signal
    return out


def normalized_pair(green_raw: list[Optional[float]], yellow_raw: list[Optional[float]]) -> tuple[list[float], list[float]]:
    """Hold-last gap fill for the PCR/Ratio lines: a bucket where one side's
    OI total was zero yields None from the ratio math; carry the previous
    value forward (and drop leading Nones on both lines together) so the
    models never see a hole. Mirrors the platform's chart behaviour."""
    n = min(len(green_raw), len(yellow_raw))
    g_out: list[float] = []
    y_out: list[float] = []
    g_last: Optional[float] = None
    y_last: Optional[float] = None
    for i in range(n):
        g = green_raw[i] if green_raw[i] is not None else g_last
        y = yellow_raw[i] if yellow_raw[i] is not None else y_last
        if g is None or y is None:
            continue  # still in the leading gap
        if not (math.isfinite(g) and math.isfinite(y)):
            continue
        g_last, y_last = g, y
        g_out.append(g)
        y_out.append(y)
    return g_out, y_out


# ── serialisation (one shape for the dashboard endpoint AND the decision trace)
_TRACE_CAP = 40


def result_to_dict(result: "MqaeResult", *, last_green: Optional[float] = None,
                   last_yellow: Optional[float] = None) -> dict:
    def _logs(items: list[ModelLog]) -> list[dict]:
        return [{"model": m.model, "text": m.text, "points": m.points} for m in items[:_TRACE_CAP]]

    return {
        "signal": result.signal,
        "total": result.total,
        "score_green": result.score_green,
        "score_yellow": result.score_yellow,
        "score_cross": result.score_cross,
        "last_green_pcr": last_green,
        "last_yellow_ratio": last_yellow,
        "logs_green": _logs(result.logs_green),
        "logs_yellow": _logs(result.logs_yellow),
        "logs_cross": _logs(result.logs_cross),
    }
