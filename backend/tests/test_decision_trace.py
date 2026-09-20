"""Per-minute decision trace (2026-09-09): every orchestrator pass emits ONE
record through the ``record_decision`` dep — stage, gates, readings with their
engine traces, candidate strikes and the outcome — without ever altering the
decision itself.

Reuses the orchestrator test harness (Fake providers, real UMP engines).
"""
from __future__ import annotations

import json
from datetime import date, datetime

from app.algo.engines import mqae, mtf_ratio, oi_structure
from app.algo.orchestrator import IndicatorEval, ZoneOrchestrator, evaluate_indicator_from_pairs
from tests.test_algo_orchestrator import MONDAY, R1_BAR, Fake, run


def _with_trace(fake: Fake):
    rows: list[dict] = []
    deps = fake.deps()
    deps.record_decision = rows.append
    return deps, rows


def test_every_pass_emits_exactly_one_row_with_stage():
    fake = Fake()
    deps, rows = _with_trace(fake)
    orch = ZoneOrchestrator(deps)
    run(orch, MONDAY)
    assert len(rows) == 1
    r = rows[0]
    assert r["ts"] == MONDAY and r["trade_date"] == MONDAY.date()
    assert r["stage"] == orch.status.state
    assert r["zone_id"] == "Z1" and r["day"] == "monday"
    assert r["unanimous"] is True
    assert r["direction"] == "CALL"
    assert r["decision"] == "reject"                    # hunting, nothing fired yet
    assert set(r["readings"]) == {"oi_change", "multi_tf", "ratio"}
    assert r["readings"]["oi_change"]["signal"] == "CALL"
    assert r["zone_snapshot"]["premium_min"] == 75.0
    # candidates were fed this minute (no bar in Fake → bar_present False)
    assert r["candidates"] and r["candidates"][0]["strike"] == 24500
    assert r["candidates"][0]["fired"] is False
    json.dumps(r, default=str)                          # JSON-safe


def test_string_readings_are_normalised_and_eval_objects_carry_detail():
    fake = Fake()
    orig = fake.deps().evaluate_indicator
    deps, rows = _with_trace(fake)

    async def ev(ind, day, zone_id, zone_cfg, symbol):
        if ind == "oi_change":
            return IndicatorEval("CALL", {"call_pts": 60.0, "put_pts": 10.0})
        return "CALL"

    deps.evaluate_indicator = ev
    orch = ZoneOrchestrator(deps)
    run(orch, MONDAY)
    assert orch.status.readings == {"oi_change": "CALL", "multi_tf": "CALL", "ratio": "CALL"}
    assert rows[0]["readings"]["oi_change"]["call_pts"] == 60.0
    assert rows[0]["readings"]["multi_tf"] == {"signal": "CALL"}
    _ = orig


def test_rejections_name_the_gate():
    fake = Fake()
    fake.readings["ratio"] = "PUT"                       # not unanimous
    deps, rows = _with_trace(fake)
    run(ZoneOrchestrator(deps), MONDAY)
    assert rows[0]["decision"] == "reject"
    assert rows[0]["unanimous"] is False
    # Signal Console F1: the trace now carries the EVIDENCE rather than the
    # name of the rule — "unanimous" would be a lie under the majority rule,
    # and it never said which way the indicators split.
    assert rows[0]["reason"] == "enabled indicators disagree (2 call, 1 put)"

    fake2 = Fake()
    fake2.config.global_.master_kill = True
    deps2, rows2 = _with_trace(fake2)
    run(ZoneOrchestrator(deps2), MONDAY)
    assert rows2[0]["stage"] == "killed" and rows2[0]["reason"] == "master kill"

    fake3 = Fake()
    fake3.strikes = []                                   # nothing inside the band
    deps3, rows3 = _with_trace(fake3)
    run(ZoneOrchestrator(deps3), MONDAY)
    assert rows3[0]["stage"] == "no_strike_in_band"
    assert "band" in rows3[0]["reason"]


def test_entry_is_recorded_as_accept_with_sizing_and_trade_id():
    fake = Fake()
    fake.bars = [R1_BAR]                                 # enters R1 on the first pass
    deps, rows = _with_trace(fake)
    orch = ZoneOrchestrator(deps)
    run(orch, MONDAY)
    assert orch.position is not None
    a = rows[-1]
    assert a["decision"] == "accept"
    assert a["trade_id"] == fake.inserted[-1]["id"]
    assert a["sizing"]["lots"] == 1 and a["sizing"]["lot_size"] == fake.lot
    assert a["sizing"]["sub_scenario"] == "R1" and "fill" in a["sizing"]
    assert a["candidates"][0]["fired"] is True and a["candidates"][0]["bar_present"] is True
    # The stage reason, then the combination evidence. Every row must answer
    # "why did the combination produce this direction?", not just rejections.
    assert a["reason"] == "entered via R1 · all 3 voting indicators agree (call)"
    # ...and the row records WHICH rule counted the votes, so it stays
    # re-derivable after the zone's setting changes.
    assert a["zone_snapshot"]["combine_rule"] == "unanimous"
    assert a["zone_snapshot"]["neutral_mode"] == "block"
    # a manage row follows while the position is open
    run(orch, MONDAY.replace(minute=31))
    assert rows[-1]["decision"] == "manage" and rows[-1]["position"] is not None
    assert rows[-1]["trade_id"] == a["trade_id"]


def test_writer_failure_never_breaks_the_pass():
    fake = Fake()
    deps = fake.deps()

    def boom(row):
        raise RuntimeError("db down")

    deps.record_decision = boom
    orch = ZoneOrchestrator(deps)
    run(orch, MONDAY)                                    # must not raise
    assert orch.status.state in ("hunting", "no_engine_data")


def test_absent_writer_is_a_noop_and_decisions_unchanged():
    a, b = Fake(), Fake()
    oa = ZoneOrchestrator(a.deps())
    db, rows = _with_trace(b)
    ob = ZoneOrchestrator(db)
    run(oa, MONDAY)
    run(ob, MONDAY)
    assert oa.status.state == ob.status.state
    assert oa.status.readings == ob.status.readings
    assert len(rows) == 1


async def test_evaluate_indicator_from_pairs_shares_serialisers():
    from app.algo.config_models import default_config
    from app.algo.series import OIChangePair, RatioPair

    zone = default_config(today=date(2026, 8, 10)).zone("monday", "Z1")
    ts = [f"2026-08-17T09:{15 + i:02d}:00+05:30" for i in range(30)]
    call = [round(0.05 * i, 2) for i in range(30)]
    put = [round(0.02 * i, 2) for i in range(30)]
    pair = OIChangePair(timestamps=ts, call_change_cr=call, put_change_cr=put,
                        strike_min=24000, strike_max=25000, spot=24500.0)

    async def oi(_w):
        return pair

    async def ratio(_w):
        return RatioPair(timestamps=ts, green_pcr=[1.0 + 0.01 * i for i in range(30)],
                         yellow_ratio=[1.0 - 0.005 * i for i in range(30)],
                         strike_min=24000, strike_max=25000, spot=24500.0)

    ev = await evaluate_indicator_from_pairs("multi_tf", zone, oi, ratio)
    assert isinstance(ev, IndicatorEval)
    assert ev.detail["basket"]["closed_minutes"] == 30
    assert ev.detail["rows"][0]["timeframe"] and "lowest_side" in ev.detail["rows"][0]
    # same shape the dashboard endpoint serialises
    rows = mtf_ratio.rows_from_cumulative_series(call, put, zone.mtf_ratio.timeframes)
    res = mtf_ratio.evaluate(rows, zone.mtf_ratio)
    assert ev.detail["rows"] == mtf_ratio.result_to_dict(res)["rows"]
    ev2 = await evaluate_indicator_from_pairs("oi_change", zone, oi, ratio)
    assert "contributions" in ev2.detail and ev2.detail["last_call_cr"] == call[-1]
    ev3 = await evaluate_indicator_from_pairs("ratio", zone, oi, ratio)
    assert "logs_green" in ev3.detail and ev3.signal in ("CALL", "PUT", "NO_TRADE")
    # serialisers are JSON-safe and capped
    json.dumps(ev.detail); json.dumps(ev2.detail); json.dumps(ev3.detail)
    assert oi_structure._TRACE_CAP == mqae._TRACE_CAP == 40


def test_decisions_to_params_serialises_json_columns():
    from app.algo.decisions import to_params

    p = to_params({"ts": datetime(2026, 8, 17, 9, 30), "trade_date": date(2026, 8, 17),
                   "stage": "hunting", "decision": "reject", "readings": {"a": {"x": 1}},
                   "gate_blocks": None})
    assert p["readings"] == '{"a": {"x": 1}}' and p["gate_blocks"] is None
    assert p["reason"] == "" and p["zone_id"] == ""
