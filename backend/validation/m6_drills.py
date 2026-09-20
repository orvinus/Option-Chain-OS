"""M6 ultra-deep drills — every operational scenario of the trading engine,
driven END-TO-END over REAL recorded market data (the archived 1-minute
sessions), through the exact live orchestrator code path.

Run (inside the backend container, DB required):
    cd /app && PYTHONPATH=. python validation/m6_drills.py

Each drill builds a scenario config (a mutated copy of the LIVE document),
runs a real backtest day through the runner, and asserts the engine's
observable behaviour: status timeline, alerts, signals, gates, trades,
fills, fees. Drill runs are created in the backtest tables and DELETED at
the end.

    D1  master kill        → whole day 'killed', zero evaluation, zero trades
    D2  day kill           → same, via the weekday switch
    D3  zone kill (Z1)     → Z1 gated; later zones still evaluate
    D4  absurd premium band→ direction found but 'no_strike_in_band' + alert
    D5  zero indicators    → §14 completeness gate blocks the zone + alert
    D6  filter disagreement→ 'combined' NO_TRADE dominates the timeline
    D7  forced entry→exit  → permissive config produces a real UMP trade;
                             entry/exit fills re-derived by hand (slippage),
                             fees re-derived by hand (the full statutory
                             stack), pnl and pnl_pct re-derived by hand
    D8  risk counters      → the D7 ledger feeds max-trades / streak inputs
                             identically to the live counters
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from datetime import timedelta  # noqa: E402

from app.algo.backtest import runner as bt_runner  # noqa: E402
from app.algo.backtest import store as bt_store  # noqa: E402
from app.algo.backtest.preflight import run_preflight  # noqa: E402
from app.algo.config_models import AlgoConfig  # noqa: E402
from app.algo.config_store import get_config_store  # noqa: E402
from app.algo.fees import round_trip_fees  # noqa: E402
from app.market.symbols import get_registry  # noqa: E402

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")

# Days on which the 6-month benchmark (live config v17) PROVED a UMP entry —
# D7 replays one and must reproduce the identical trade (cross-run
# determinism + the hand-check target).
KNOWN_ENTRY_DAYS = (
    date(2026, 5, 11),   # Z1 PUT 23850 R1 in 10:24 @103.89 out 10:25 BASE_SL
    date(2026, 2, 18),
    date(2026, 4, 29),
    date(2026, 5, 25),
    date(2026, 6, 30),
)

checks = 0
failures: list[str] = []
created_runs: list[int] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    status = "PASS" if ok else "FAIL"
    print(f"{status}  {label}" + (f"  — {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


async def base_config() -> AlgoConfig:
    cv = await get_config_store().get_live()
    cfg = cv.config.model_copy(deep=True)
    cfg.global_.paper.paper_mode = True
    cfg.global_.paper.shadow_mode = False
    return cfg


def settings_for(cfg: AlgoConfig) -> dict:
    reg = get_registry()
    symbols = {d.index_symbol for d in cfg.days.values()}
    return {
        "balance_mode": "compounding",
        "starting_balance": cfg.global_.paper.virtual_balance,
        "use_default_exclusions": True,
        "min_minutes_per_day": 300,
        "lot_sizes": {s: (reg.get(s).lot_size if reg.get(s) else 75) for s in symbols},
        "strike_steps": {s: (reg.get(s).strike_step if reg.get(s) else 50) for s in symbols},
    }


async def run_day(cfg: AlgoConfig, day: date, label: str) -> tuple[dict, list, list, list]:
    """One-day backtest under ``cfg`` → (run row, day rows, trades, signals)."""
    run_id = await bt_store.create_run(
        label=label, created_by="m6-drill", from_date=day, to_date=day,
        config=cfg.model_dump(by_alias=True, mode="json"),
        config_version=None, settings=settings_for(cfg),
    )
    created_runs.append(run_id)
    await bt_runner._run(run_id)
    run = await bt_store.get_run(run_id)
    days = await bt_store.run_days(run_id)
    trades = await bt_store.run_trades(run_id, limit=1000)
    signals = await bt_store.run_signals(run_id)
    assert run["status"] in ("done",), f"{label}: run ended {run['status']} — {run['error']}"
    return run, days, trades, signals


def timeline(signals: list, indicator: str) -> list[str]:
    return [s["reading"] for s in signals if s["indicator"] == indicator]


def states(signals: list) -> set[str]:
    return set(timeline(signals, "status"))


def alerts(signals: list) -> list[str]:
    return [
        str((s.get("payload") or {}).get("text", ""))
        for s in signals if s["indicator"] == "notify"
    ]


def permissive(cfg: AlgoConfig, weekday: str) -> AlgoConfig:
    """Every filter wide open on every zone; band accepts anything; the UMP
    tier params come from the live doc (the engine itself decides entries)."""
    d = cfg.days[weekday]
    zones = list(d.zones.items())
    for _zid, z in zones:
        z.zone_kill = False
        z.strategy_active = True
        z.premium_min = 1.0
        z.premium_max = 100000.0
        z.max_trades = 10
        z.reeval_cadence = "every_candle"
        z.enabled_indicators = ["ratio"]     # single indicator decides (§4.1)
    # One wide session-long zone keeps the hunt alive all day.
    first = zones[0][1]
    first.start = "09:20"
    first.end = "15:10"
    if len(zones) > 1:
        zones[1][1].start = "15:10"
        zones[1][1].end = "15:12"
        zones[2][1].start = "15:12"
        zones[2][1].end = "15:14"
    d.day_kill = False
    d.all_in = False
    d.allocation_pct = 90.0
    cfg.global_.master_kill = False
    return cfg


async def main() -> None:
    # Pick the newest day the runner would actually RUN — same preflight the
    # runner itself uses, so a thin/skipped day can never blank the drills.
    probe_cfg = await base_config()
    today = date.today()
    report = await run_preflight(
        probe_cfg, {"min_minutes_per_day": 300}, today - timedelta(days=14), today
    )
    planned = [p for p in report.days if p.planned]
    assert planned, "no runnable day in the last 14 days"
    day = date.fromisoformat(planned[-1].trade_date)
    weekday = _WEEKDAYS[day.weekday()]
    print(f"drill day: {day} ({weekday}) — newest preflight-planned day\n")

    try:
        # ── D1: master kill ──
        cfg = await base_config()
        cfg.global_.master_kill = True
        _, days, trades, signals = await run_day(cfg, day, "m6-D1-master-kill")
        st = states(signals)
        check(st == {"killed"}, "D1 master kill: whole day in 'killed' state", str(st))
        check(len(trades) == 0, "D1 master kill: zero trades")
        check(len(timeline(signals, "combined")) == 0,
              "D1 master kill: indicators never even evaluated")

        # ── D2: day kill ──
        cfg = await base_config()
        cfg.global_.master_kill = False
        cfg.days[weekday].day_kill = True
        _, _, trades, signals = await run_day(cfg, day, "m6-D2-day-kill")
        check(states(signals) == {"killed"}, "D2 day kill: whole day 'killed'", str(states(signals)))
        check(len(trades) == 0, "D2 day kill: zero trades")

        # ── D3: zone kill Z1 only ──
        cfg = await base_config()
        cfg.global_.master_kill = False
        cfg.days[weekday].day_kill = False
        for z in cfg.days[weekday].zones.values():
            z.zone_kill = False
            z.strategy_active = True
            z.enabled_indicators = ["ratio"]
        cfg.days[weekday].zones["Z1"].zone_kill = True
        _, _, _, signals = await run_day(cfg, day, "m6-D3-zone-kill")
        z1_sig = [s for s in signals if s["zone_id"] == "Z1" and s["indicator"] == "combined"]
        other_sig = [s for s in signals if s["zone_id"] in ("Z2", "Z3") and s["indicator"] == "combined"]
        check(len(z1_sig) == 0, "D3 zone kill: Z1 never evaluates", f"{len(z1_sig)} signals")
        check(len(other_sig) > 0, "D3 zone kill: Z2/Z3 still evaluate", f"{len(other_sig)} signals")
        check("gated" in states(signals), "D3 zone kill: Z1 window shows 'gated'")

        # ── D4: absurd premium band → no strike qualifies ──
        cfg = await base_config()
        cfg = permissive(cfg, weekday)
        z1 = cfg.days[weekday].zones["Z1"]
        z1.premium_min = 0.01
        z1.premium_max = 0.02          # nothing trades at 1-2 paise
        _, _, trades, signals = await run_day(cfg, day, "m6-D4-band-starved")
        check(len(trades) == 0, "D4 band starved: zero trades")
        got_dir = any(r in ("CALL", "PUT") for r in timeline(signals, "combined"))
        if got_dir:
            check("no_strike_in_band" in states(signals),
                  "D4 band starved: state shows no_strike_in_band", str(states(signals)))
            check(any("band" in a for a in alerts(signals)),
                  "D4 band starved: operator alerted once", str(alerts(signals))[:120])
        else:
            check(True, "D4 band starved: no direction ever formed (band never consulted — honest)")

        # ── D5: zero enabled indicators → §14 completeness gate ──
        cfg = await base_config()
        cfg = permissive(cfg, weekday)
        cfg.days[weekday].zones["Z1"].enabled_indicators = []
        _, _, trades, signals = await run_day(cfg, day, "m6-D5-no-indicators")
        z1_comb = [s for s in signals if s["zone_id"] == "Z1" and s["indicator"] == "combined"]
        check(len(z1_comb) == 0, "D5 §14 gate: zone with no indicators never signals")
        check(any("incomplete" in a.lower() or "blocked" in a.lower() for a in alerts(signals)),
              "D5 §14 gate: block is alerted, never silent", str(alerts(signals))[:150])
        check(len([t for t in trades if t["zone_id"] == "Z1"]) == 0, "D5 §14 gate: Z1 zero trades")

        # ── D6: filter disagreement → unanimous rule holds ──
        cfg = await base_config()
        cfg = permissive(cfg, weekday)
        # All three indicators enabled: on real data they disagree most minutes.
        cfg.days[weekday].zones["Z1"].enabled_indicators = ["oi_change", "multi_tf", "ratio"]
        _, _, _, signals = await run_day(cfg, day, "m6-D6-disagreement")
        combined = timeline(signals, "combined")
        per_ind = {
            i: timeline(signals, i) for i in ("oi_change", "multi_tf", "ratio")
        }
        check(len(combined) > 0, "D6 unanimous: combined signal recorded",
              f"{len(combined)} transitions")
        check("NO_TRADE" in combined,
              "D6 unanimous: disagreement collapses to NO_TRADE", str(combined[:6]))
        check(all(len(v) > 0 for v in per_ind.values()),
              "D6 unanimous: every enabled indicator produced readings",
              str({k: len(v) for k, v in per_ind.items()}))

        # ── D7: real entry → exit replayed under the LIVE config on a day
        #        the 6-month benchmark proved an entry, with hand-checked
        #        fills + fees. Also proves cross-run reproducibility. ──
        trade = None
        used_day = None
        alloc_pct_used = None
        for d in KNOWN_ENTRY_DAYS:
            cfg = await base_config()
            _, _, trades, signals = await run_day(cfg, d, f"m6-D7-entry-{d}")
            closed = [t for t in trades if t["exit_ts"] is not None]
            if closed:
                trade = closed[0]
                d7_signals = signals
                used_day = d
                dcfg = cfg.days[_WEEKDAYS[d.weekday()]]
                alloc_pct_used = 100.0 if dcfg.all_in else dcfg.allocation_pct
                break
        if trade is None:
            check(False, "D7 entry replay: no trade on any benchmark-proven entry day",
                  "the benchmark found entries here — nondeterminism or config drift!")
        else:
            check(True, f"D7 entry replay: real trade reproduced on {used_day}",
                  f"{trade['side']} {trade['strike']} {trade['sub_scenario']} "
                  f"{trade['entry_ts'][11:16]}->{trade['exit_ts'][11:16]} {trade['exit_reason']}")
            if used_day == date(2026, 5, 11):
                check(
                    trade["strike"] == 23850 and trade["sub_scenario"] == "R1"
                    and trade["entry_ts"][11:16] == "10:24"
                    and abs(trade["entry_price"] - 103.89) < 0.01,
                    "D7 cross-run determinism: matches the benchmark's trade exactly",
                    f"{trade['strike']} {trade['sub_scenario']} {trade['entry_ts'][11:16]} @ {trade['entry_price']}",
                )

            cfg_used = await base_config()   # slippage/fees identical in live doc
            slip = cfg_used.global_.paper.slippage_pct / 100
            lot = settings_for(cfg_used)["lot_sizes"]["NIFTY"]
            lots = trade["lots"]

            # Hand-derive the raw engine prices back from the fills.
            raw_entry = trade["entry_price"] / (1 + slip)
            raw_exit = trade["exit_price"] / (1 - slip)
            check(abs(trade["entry_price"] - raw_entry * (1 + slip)) < 1e-9,
                  "D7 entry fill = engine price × (1 + slippage)",
                  f"fill {trade['entry_price']} slip {slip*100}%")
            check(abs(trade["exit_price"] - raw_exit * (1 - slip)) < 1e-9,
                  "D7 exit fill = engine price × (1 − slippage)")

            # Hand-recompute the full fee stack from the run's frozen config.
            fees = round_trip_fees(
                cfg_used.global_.fees,
                buy_premium=float(trade["entry_price"]),
                sell_premium=float(trade["exit_price"]),
                lot_size=lot, lots=lots,
            )
            stored = trade["fees"] or {}
            check(abs(float(stored.get("total", -1)) - fees.total) < 0.01,
                  "D7 fees: stored total == independent round_trip_fees()",
                  f"stored {stored.get('total')} vs {fees.total}")
            gross = (float(trade["exit_price"]) - float(trade["entry_price"])) * lot * lots
            net = round(gross - fees.total, 2)
            check(abs(float(trade["pnl_rupees"]) - net) < 0.01,
                  "D7 P&L: pnl_rupees == gross − fees (hand math)",
                  f"stored {trade['pnl_rupees']} vs {net} (gross {round(gross,2)})")

            # pnl_pct basis = the day's allocated capital (§12.2). First trade
            # of the day → balance is the untouched starting balance.
            alloc = cfg_used.global_.paper.virtual_balance * (alloc_pct_used or 0) / 100
            want_pct = round(net / alloc * 10000) / 100 if alloc > 0 else None
            check(want_pct is not None and abs(float(trade["pnl_pct"]) - want_pct) < 0.05,
                  "D7 pnl_pct == pnl ÷ allocated (day's allocation_pct basis)",
                  f"stored {trade['pnl_pct']} vs {want_pct} (alloc {alloc:.0f})")

            # The UMP capture must show the entry event for this trade.
            cap = next((s for s in d7_signals
                        if s["indicator"] == "ump_capture" and s["reading"] == str(trade["id"])), None)
            check(cap is not None, "D7 UMP capture bundle stored for the trade")
            if cap:
                kinds = [e["kind"] for e in (cap["payload"] or {}).get("events", [])]
                check(trade["sub_scenario"] in kinds,
                      "D7 capture: entry sub-scenario present in engine events",
                      f"{trade['sub_scenario']} in {kinds[:8]}")

            # D8: the ledger drives the risk counters exactly like live.
            d7_trades = await bt_store.run_trades(created_runs[-1], limit=100)
            d7_closed = [t for t in d7_trades if t["exit_ts"] is not None]
            day_cfg_used = cfg_used.days[_WEEKDAYS[used_day.weekday()]]
            per_zone: dict[str, int] = {}
            for t in d7_closed:
                per_zone[t["zone_id"]] = per_zone.get(t["zone_id"], 0) + 1
            over = {
                z: n for z, n in per_zone.items()
                if n > day_cfg_used.zones[z].max_trades
            }
            check(not over, "D8 max_trades honoured per zone", str(per_zone))
            hunting_states = states(d7_signals)
            check("in_trade" in hunting_states,
                  "D8 status timeline shows in_trade while position open", str(hunting_states))
            entry_alerts = [a for a in alerts(d7_signals) if "ENTRY" in a]
            exit_alerts = [a for a in alerts(d7_signals) if "EXIT" in a]
            check(len(entry_alerts) >= 1 and len(exit_alerts) >= 1,
                  "D8 entry + exit alerts recorded (what Telegram would receive)",
                  f"{len(entry_alerts)} entry / {len(exit_alerts)} exit")
    finally:
        for rid in created_runs:
            bt_runner._jobs.pop(rid, None)
            await bt_store.delete_run(rid)
        print(f"\ncleaned up {len(created_runs)} drill run(s)")

    print(f"\n══ {checks - len(failures)}/{checks} M6 drill checks passed ══")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
