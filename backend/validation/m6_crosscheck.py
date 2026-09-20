"""M6 cross-check — the backtest's recorded signal timeline vs the LIVE
evaluation path, transition by transition.

Run (inside the backend container, after a benchmark run exists):
    cd /app && PYTHONPATH=. python validation/m6_crosscheck.py <run_id> <YYYY-MM-DD>

For EVERY indicator transition the backtest recorded on that day, this
recomputes the reading through the live series builders (SQL) + the pure
engines, under the run's pinned config, with the window clamped exactly as
the live instant saw it (closed data only). Every recomputed reading must
EQUAL the recorded one — proving the backtest deps and the live path are one
pipeline end-to-end, not just at the series layer.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, time, timedelta

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.algo import series as ser  # noqa: E402
from app.algo.backtest import store as bt_store  # noqa: E402
from app.algo.backtest.data import load_expiry_map  # noqa: E402
from app.algo.config_models import AlgoConfig  # noqa: E402
from app.algo.engines import mqae, mtf_ratio, oi_structure  # noqa: E402
from app.core.time_utils import IST, ist_naive_to_utc  # noqa: E402

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")

checks = 0
failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    if not ok:
        failures.append(label)
        print(f"FAIL  {label}  — {detail}")


async def live_reading(
    ind: str, cfg: AlgoConfig, weekday: str, zone_id: str,
    symbol: str, expiry: date, day: date, eval_minute_ist: datetime,
) -> str:
    """Exactly _runtime_deps.evaluate_indicator, with the historical window
    the live instant would have had: closed minutes ≤ eval_minute − 1."""
    zone_cfg = cfg.days[weekday].zones[zone_id]
    from_utc = ist_naive_to_utc(datetime.combine(day, time(9, 15)))
    closed_to = ist_naive_to_utc(eval_minute_ist) - timedelta(microseconds=1)
    now_utc = ist_naive_to_utc(eval_minute_ist)

    if ind == "oi_change":
        pair = await ser.build_oi_change_pair(
            symbol, expiry, zone_cfg.oi_structure.strikes_atm_window,
            from_ts=from_utc, to_ts=closed_to, now=now_utc,
        )
        if pair is None or len(pair.call_change_cr) < 2:
            return "NO_TRADE"
        return oi_structure.evaluate(
            pair.call_change_cr, pair.put_change_cr, zone_cfg.oi_structure
        ).signal
    if ind == "multi_tf":
        pair = await ser.build_oi_change_pair(
            symbol, expiry, zone_cfg.mtf_ratio.strikes_atm_window,
            from_ts=from_utc, to_ts=closed_to, now=now_utc,
        )
        if pair is None or len(pair.call_change_cr) < 2:
            return "NO_TRADE"
        rows = mtf_ratio.rows_from_cumulative_series(
            pair.call_change_cr, pair.put_change_cr, zone_cfg.mtf_ratio.timeframes
        )
        return mtf_ratio.evaluate(rows, zone_cfg.mtf_ratio).reading
    pair2 = await ser.build_ratio_pair(
        symbol, expiry, zone_cfg.mqae.strikes_atm_window,
        from_ts=from_utc, to_ts=closed_to, now=now_utc,
    )
    if pair2 is None or len(pair2.green_pcr) < 2:
        return "NO_TRADE"
    return mqae.evaluate(pair2.green_pcr, pair2.yellow_ratio, zone_cfg.mqae).signal


async def main() -> None:
    run_id = int(sys.argv[1]) if len(sys.argv) > 1 else 9
    day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date(2026, 5, 11)

    run = await bt_store.get_run(run_id, include_config=True)
    assert run is not None, f"run {run_id} not found"
    cfg = AlgoConfig.model_validate(run["config"])
    weekday = _WEEKDAYS[day.weekday()]
    symbol = cfg.days[weekday].index_symbol
    em = await load_expiry_map([symbol])
    expiry = em.for_day(symbol, day)
    assert expiry is not None

    signals = await bt_store.run_signals(run_id, day.isoformat())
    transitions = [
        s for s in signals
        if s["indicator"] in ("oi_change", "multi_tf", "ratio") and s["zone_id"]
    ]
    print(f"run #{run_id} {day} ({weekday}, {symbol} exp {expiry}) — "
          f"{len(transitions)} indicator transitions to cross-check\n")
    assert transitions, "no indicator transitions recorded that day"

    for s in transitions:
        # Stored ts carries the simulated IST wall clock in its HH:MM.
        hh, mm = int(s["ts"][11:13]), int(s["ts"][14:16])
        eval_minute = datetime.combine(day, time(hh, mm))
        got = await live_reading(
            s["indicator"], cfg, weekday, s["zone_id"], symbol, expiry, day, eval_minute
        )
        check(
            got == s["reading"],
            f"{s['ts'][11:16]} {s['zone_id']} {s['indicator']}",
            f"backtest={s['reading']} live-path={got}",
        )

    print(f"══ {checks - len(failures)}/{checks} transitions match the live path ══")
    if failures:
        raise SystemExit(f"{len(failures)} mismatch(es)")


if __name__ == "__main__":
    asyncio.run(main())
