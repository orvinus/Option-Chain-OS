"""The Master Quantitative Action Engine PDS, reproduced as tests.

Every worked example and rule in "Master_Quantitative_Action_Engine_Documentation"
(v1.0) that has a checkable number, pinned against app.algo.engines.mqae. Section
numbers refer to the PDS. (Exact parity with the reference ratio.html engine is
proven separately: 180/180 cases identical, 2026-09-15.)
"""
from __future__ import annotations

from app.algo.config_models import MqaeParams
from app.algo.engines import mqae

FEATS = ("velocity_on", "order_blocks_on", "smc_on", "trend_rider_on", "crossover_on")


def only(feature: str, mode: str = "aggressive", **kw) -> MqaeParams:
    kw.setdefault("master_threshold", 3 if mode == "aggressive" else 6)
    return MqaeParams(**{f: (f == feature) for f in FEATS}, risk_mode=mode, **kw)


def ramp(a: float, b: float, n: int) -> list[float]:
    return [round(a + (b - a) * i / (n - 1), 6) for i in range(n)]


# ── §6 Kinetic Velocity ──────────────────────────────────────────────────────
def test_s6_velocity_example_bar_100() -> None:
    """Green 0.520 at bar 92, 0.650 at bar 100, period 8 → 0.130 > 0.012 →
    +3 Aggressive, +1 Conservative."""
    green = [0.5] * 101
    green[92], green[100] = 0.520, 0.650
    yellow = [1.0] * 101                              # flat → no yellow velocity
    for mode, want in (("aggressive", 3.0), ("conservative", 1.0)):
        r = mqae.evaluate(green, yellow, only("velocity_on", mode, velocity_period=8, velocity_surge_threshold=0.012))
        assert abs(r.green_velocity[-1] - 0.130) < 1e-12
        assert r.score_green == want and [l.text for l in r.logs_green] == ["Velocity UP Surge"]
        assert r.score_yellow == 0.0


def test_s6_yellow_velocity_is_inverted_and_insufficient_history_is_zero() -> None:
    green = [1.0] * 20
    yellow = ramp(1.0, 1.2, 20)                       # rising yellow = bearish
    r = mqae.evaluate(green, yellow, only("velocity_on", velocity_period=8, velocity_surge_threshold=0.012))
    assert r.score_yellow == -3.0 and r.logs_yellow[0].text == "Velocity UP Surge"
    assert r.yellow_velocity[:8] == [0.0] * 8         # i < period → 0


# ── §7 Order Blocks ──────────────────────────────────────────────────────────
def test_s7_demand_block_example() -> None:
    """Swing low 0.800 → swing high 0.850 (impulse 0.050 ≥ 0.02) → DEMAND
    top 0.810 / bottom 0.800; price 0.805 inside → +3 CALL (Aggressive)."""
    green = ramp(0.90, 0.80, 6) + ramp(0.81, 0.85, 5) + ramp(0.84, 0.806, 8) + [0.805, 0.805, 0.805, 0.805]
    r = mqae.evaluate(green, [1.0] * len(green), only("order_blocks_on", ob_impulse_trigger=0.02, ob_zone_width=0.01))
    demand = [b for b in r.green_blocks if b.type == "DEMAND"]
    assert any(abs(b.bot - 0.800) < 1e-9 and abs(b.top - 0.810) < 1e-9 for b in demand), r.green_blocks
    assert r.score_green == 3.0 and "Touching DEMAND Block" in [l.text for l in r.logs_green]


def test_s7_block_is_invalidated_when_price_breaks_through() -> None:
    green = ramp(0.90, 0.80, 6) + ramp(0.81, 0.85, 5) + ramp(0.84, 0.79, 8) + [0.805] * 4
    r = mqae.evaluate(green, [1.0] * len(green), only("order_blocks_on", ob_impulse_trigger=0.02, ob_zone_width=0.01))
    assert not any(abs(b.bot - 0.800) < 1e-9 and b.type == "DEMAND" for b in r.green_blocks)
    assert r.score_green == 0.0


# ── §8 SMC Structure ─────────────────────────────────────────────────────────
def test_s8_smc_example_hl_then_hh_is_bullish() -> None:
    """Low 0.700, High 0.900, Low 0.750 (HL), High 0.950 (HH) → last swing HH →
    +3 Green in Conservative (×3), +1 in Aggressive."""
    green = (ramp(0.80, 0.70, 6) + ramp(0.72, 0.90, 8) + ramp(0.88, 0.75, 8)
             + ramp(0.77, 0.95, 8) + ramp(0.94, 0.93, 4))
    for mode, want in (("conservative", 3.0), ("aggressive", 1.0)):
        r = mqae.evaluate(green, [1.0] * len(green), only("smc_on", mode, smc_swing_bars=3, smc_min_move=0.025))
        labels = [(s.label, round(s.value, 3)) for s in r.green_swings]
        assert labels[-3:] == [("H", 0.9), ("HL", 0.75), ("HH", 0.95)], labels
        assert r.score_green == want and r.logs_green[0].text == "SMC Structure (HH)"


# ── §9 Macro Trend Rider ─────────────────────────────────────────────────────
def test_s9_rider_is_fixed_plus_minus_one_in_both_modes() -> None:
    up = ramp(0.5, 1.0, 60)
    down = ramp(1.0, 0.5, 60)
    for mode in ("aggressive", "conservative"):
        intact = mqae.evaluate(up, up, only("trend_rider_on", mode))
        assert intact.score_green == 1.0 and intact.logs_green[0].text == "Macro Rider Intact"
        assert intact.score_yellow == -1.0                     # inverse
        broken = mqae.evaluate(down, down, only("trend_rider_on", mode))
        assert broken.score_green == -1.0 and broken.logs_green[0].text == "Macro Rider Broken"
        assert broken.score_yellow == 1.0


# ── §10 Crossover Momentum ───────────────────────────────────────────────────
def test_s10_crossover_example() -> None:
    """Green 0.780 > Yellow 0.750 → +1 Aggressive, +3 Conservative."""
    for mode, want in (("aggressive", 1.0), ("conservative", 3.0)):
        r = mqae.evaluate([0.780] * 5, [0.750] * 5, only("crossover_on", mode))
        assert r.score_cross == want and r.logs_cross[0].text == "Green > Yellow"
    r = mqae.evaluate([0.70] * 5, [0.75] * 5, only("crossover_on"))
    assert r.score_cross == -1.0 and r.logs_cross[0].text == "Yellow > Green"


# ── §4 weights · §11 kill switches · §12 synthesis ──────────────────────────
def test_s4_weight_table_per_mode() -> None:
    green = [0.5] * 101
    green[92], green[100] = 0.520, 0.650
    want = {"aggressive": {"velocity_on": 3.0, "crossover_on": 1.0},
            "conservative": {"velocity_on": 1.0, "crossover_on": 3.0}}
    for mode, per in want.items():
        assert mqae.evaluate(green, [1.0] * 101, only("velocity_on", mode)).score_green == per["velocity_on"]
        assert mqae.evaluate([0.8] * 5, [0.7] * 5, only("crossover_on", mode)).score_cross == per["crossover_on"]


def test_s11_kill_switch_off_excludes_the_model() -> None:
    green = [0.5] * 101
    green[92], green[100] = 0.520, 0.650
    off = MqaeParams(**{f: False for f in FEATS})
    r = mqae.evaluate(green, [1.0] * 101, off)
    assert (r.score_green, r.score_yellow, r.score_cross, r.signal) == (0.0, 0.0, 0.0, "NO_TRADE")
    assert r.green_velocity[-1] > 0          # §11: oscillator still renders when Velocity is OFF


def test_s12_total_and_signal_thresholds() -> None:
    green = [0.5] * 101
    green[92], green[100] = 0.520, 0.650
    # Aggressive, velocity +3 on green, crossover: green 0.650 < yellow 1.0 → -1 → total +2 < 3
    p = MqaeParams(**{f: f in ("velocity_on", "crossover_on") for f in FEATS}, risk_mode="aggressive", master_threshold=3)
    r = mqae.evaluate(green, [1.0] * 101, p)
    assert r.total == r.score_green + r.score_yellow + r.score_cross == 2.0 and r.signal == "NO_TRADE"
    p2 = p.model_copy(update={"master_threshold": 2})
    assert mqae.evaluate(green, [1.0] * 101, p2).signal == "CALL"          # total ≥ threshold
    down = [0.5] * 101
    down[92], down[100] = 0.650, 0.520
    p3 = only("velocity_on", master_threshold=3)
    assert mqae.evaluate(down, [1.0] * 101, p3).signal == "PUT"             # total ≤ −threshold


# ── §5 / §14.1 history ───────────────────────────────────────────────────────
def test_s14_history_starts_at_bar_30_and_records_transitions_only() -> None:
    green = [0.5] * 60
    for i in range(40, 60):
        green[i] = 0.5 + 0.02 * (i - 39)                                  # surge from bar 40
    h = mqae.history_transitions(green, [1.0] * 60, only("velocity_on", velocity_period=8, velocity_surge_threshold=0.012))
    assert h[0].bar == 30 and h[0].signal == "NO_TRADE"                   # first evaluated state always records
    assert all(a.signal != b.signal for a, b in zip(h, h[1:]))            # only transitions
    assert [x.signal for x in h] == ["NO_TRADE", "CALL"]
