"""MQAE — golden-vector parity + behaviour tests.

Run:  cd backend && PYTHONPATH=. python tests/test_mqae_golden.py

Fixtures (tests/fixtures/mqae_golden.json) come from running the VERBATIM
reference implementation (the JS preserved in "ratio html code.docx") under
node: the phase-scripted synthetic day in both risk modes, every kill switch
in isolation, a threshold variation, a hand-crafted order-block scenario, a
flat series, a below-lookback series, and a full history-transition replay.
Each fixture embeds its series; parity is asserted on scores, signal, swing
labels, active order blocks, rider state and the transition list.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.algo.config_models import MqaeParams
from app.algo.engines import mqae as eng

_FIXTURE = Path(__file__).parent / "fixtures" / "mqae_golden.json"


def _params_from_cfg(cfg: dict) -> MqaeParams:
    return MqaeParams(
        risk_mode="aggressive" if cfg["riskMode"] == "AGGRESSIVE" else "conservative",
        master_threshold=cfg["mThresh"],
        velocity_on=cfg["useVel"],
        velocity_period=cfg["vPeriod"],
        velocity_surge_threshold=cfg["vThresh"],
        order_blocks_on=cfg["useOB"],
        ob_impulse_trigger=cfg["obImpulse"],
        ob_zone_width=cfg["obWidth"],
        smc_on=cfg["useSMC"],
        smc_swing_bars=cfg["sBars"],
        smc_min_move=cfg["sMin"],
        trend_rider_on=cfg["useRider"],
        trail_period=cfg["trPeriod"],
        trail_buffer=cfg["trBuffer"],
        crossover_on=cfg["useCross"],
    )


def _sig(js_sig: str) -> str:
    return "NO_TRADE" if js_sig == "NO TRADE" else js_sig


def test_golden_parity_all_scenarios():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8-sig"))
    assert len(data["fixtures"]) >= 12
    for fx in data["fixtures"]:
        params = _params_from_cfg(fx["cfg"])
        res = eng.evaluate(fx["green"], fx["yellow"], params)
        exp = fx["expected"]
        name = fx["name"]

        assert res.signal == _sig(exp["sig"]), (
            f"{name}: signal {res.signal} != reference {_sig(exp['sig'])}"
        )
        assert res.total == exp["total"], f"{name}: total {res.total} != {exp['total']}"
        assert res.score_green == exp["scoreG"], f"{name}: scoreG"
        assert res.score_yellow == exp["scoreY"], f"{name}: scoreY"
        assert res.score_cross == exp["scoreCross"], f"{name}: scoreCross"

        got_sw_g = [(s.value, s.type, s.label) for s in res.green_swings]
        want_sw_g = [(s["val"], s["t"], s["label"]) for s in exp["swingsG"]]
        assert got_sw_g == want_sw_g, f"{name}: green swings diverged"
        got_sw_y = [(s.value, s.type, s.label) for s in res.yellow_swings]
        want_sw_y = [(s["val"], s["t"], s["label"]) for s in exp["swingsY"]]
        assert got_sw_y == want_sw_y, f"{name}: yellow swings diverged"

        got_ob_g = [(o.type, o.top, o.bot) for o in res.green_blocks]
        want_ob_g = [(o["type"], o["top"], o["bot"]) for o in exp["obG"]]
        assert got_ob_g == want_ob_g, f"{name}: green active order blocks diverged"
        got_ob_y = [(o.type, o.top, o.bot) for o in res.yellow_blocks]
        want_ob_y = [(o["type"], o["top"], o["bot"]) for o in exp["obY"]]
        assert got_ob_y == want_ob_y, f"{name}: yellow active order blocks diverged"

        for side, rider, want in (
            ("green", res.green_rider, exp["riderLastG"]),
            ("yellow", res.yellow_rider, exp["riderLastY"]),
        ):
            if want is None:
                assert not rider, f"{name}: {side} rider should be empty"
            else:
                assert rider, f"{name}: {side} rider missing"
                assert abs(rider[-1].val - want["val"]) < 1e-9, f"{name}: {side} rider trail"
                assert rider[-1].trend == want["trend"], f"{name}: {side} rider trend"

        if "history" in exp:
            got_hist = [(h.bar, h.signal, h.total) for h in
                        eng.history_transitions(fx["green"], fx["yellow"], params)]
            want_hist = [(h["bar"], _sig(h["sig"]), h["total"]) for h in exp["history"]]
            assert got_hist == want_hist, (
                f"{name}: history transitions diverged\n got {got_hist}\nwant {want_hist}"
            )
    print(f"golden parity: {len(data['fixtures'])} scenarios agree with the reference")


# ── behaviour cases beyond the fixtures ───────────────────────────────────

def test_mode_weights_rewire():
    # A velocity-only surge scores 3 in aggressive, 1 in conservative.
    green = [1.0] * 20 + [1.5]     # big final jump → UP surge
    yellow = [1.0] * 21
    base = dict(order_blocks_on=False, smc_on=False, trend_rider_on=False, crossover_on=False)
    agg = eng.evaluate(green, yellow, MqaeParams(risk_mode="aggressive", **base))
    con = eng.evaluate(green, yellow, MqaeParams(risk_mode="conservative", **base))
    assert agg.score_green == 3.0 and con.score_green == 1.0
    # Same data: aggressive threshold 3 fires CALL, conservative 6 does not.
    assert agg.signal == "CALL" and con.signal == "NO_TRADE"


def test_yellow_scores_inverse():
    # Surge on the YELLOW line only → contributes toward PUT.
    green = [1.0] * 21
    yellow = [1.0] * 20 + [1.5]
    res = eng.evaluate(green, yellow, MqaeParams(
        order_blocks_on=False, smc_on=False, trend_rider_on=False, crossover_on=False,
    ))
    assert res.score_yellow == -3.0 and res.score_green == 0.0


def test_rider_is_fixed_weight_in_both_modes():
    # Green trends up (rider Intact → +1); Yellow trends DOWN, so its rider
    # flips to Broken, which on the inverse line ALSO scores +1 (bullish).
    # The point under test: the magnitude stays exactly 1 in BOTH risk modes.
    green = list(x / 100 for x in range(100, 160))
    yellow = list(reversed(green))
    for mode in ("aggressive", "conservative"):
        res = eng.evaluate(green, yellow, MqaeParams(
            risk_mode=mode, velocity_on=False, order_blocks_on=False,
            smc_on=False, crossover_on=False,
        ))
        assert res.score_green == 1.0, f"{mode}: rider must contribute exactly +1"
        assert res.score_yellow == 1.0, (
            f"{mode}: broken rider on the inverse line must be exactly +1"
        )


def test_smc_scores_latest_swing_only():
    # Two swings: an early high then a final LOWER high (LH). Only the LH
    # (latest) may score — deliberately unlike the OI Structure engine.
    green = [1.0, 1.4, 1.0, 0.6, 1.0, 1.2, 1.0, 0.9, 0.95, 0.9]
    res = eng.evaluate(green, [2.0] * len(green), MqaeParams(
        velocity_on=False, order_blocks_on=False, trend_rider_on=False,
        crossover_on=False, smc_swing_bars=1, smc_min_move=0.01,
    ))
    smc_logs = [l for l in res.logs_green if l.model == "smc"]
    assert len(smc_logs) == 1, "exactly ONE swing (the latest) scores"


def test_never_both_modes_off_is_static():
    # The config model's Literal["aggressive","conservative"] makes a
    # both-off state unrepresentable — the reference enforces this in its
    # toggle handler; here it is a type-level guarantee.
    from typing import get_args
    modes = get_args(MqaeParams.model_fields["risk_mode"].annotation)
    assert set(modes) == {"aggressive", "conservative"}


def test_normalized_pair_holds_last_and_drops_leading_gaps():
    g, y = eng.normalized_pair(
        [None, 1.0, None, 1.2, 1.3],
        [2.0, 2.0, 2.1, None, 2.3],
    )
    # index0 dropped (green leading gap); index2 green holds 1.0; index3
    # yellow holds 2.1.
    assert g == [1.0, 1.0, 1.2, 1.3]
    assert y == [2.0, 2.1, 2.1, 2.3]


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
    print("all MQAE golden tests passed")


if __name__ == "__main__":
    _run_all()
