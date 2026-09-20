"""Our side of the Pine-parity harness: dump the same intermediates the Pine
debug export logs (daily/weekly inputs, 1H reversal array, level set, every
engine event) for N strikes × CE/PE under Pine DEFAULT inputs.

Runs INSIDE the backend image (needs the DB and ``app``):

    docker exec docker-backend-1 python -m scripts.parity.dump \\
        --symbol NIFTY --expiry 2026-09-08 --strikes 23700,23750 --ots CE,PE \\
        --bar5 --timeout-bars 1 --out /tmp/parity

``--bar5`` = Pine-HISTORICAL emulation: TradingView evaluates a script ONCE
per completed chart bar with that bar's final OHLC, so each 5-minute window
is collapsed into one 'minute' stamped at window+4 and the engine performs
exactly one confirmed-close evaluation per 5m candle. ``--timeout-bars``
must match the chart's "Trigger Timeout" input (the user's chart runs 1;
Pine's default is 6).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_PKG = ROOT / "backend" if (ROOT / "backend" / "app").is_dir() else ROOT
sys.path.insert(0, str(_PKG))

from app.algo.config_models import UmpParams  # noqa: E402
from app.algo.engines.ump.levels import detect_h1_reversals  # noqa: E402
from app.algo.series import PremiumMinute, build_premium_life  # noqa: E402
from app.api.algo_engines import replay_ump  # noqa: E402

IST_D = timedelta(hours=5, minutes=30)


def fold_5m(minutes: list[PremiumMinute]) -> list[PremiumMinute]:
    out: list[PremiumMinute] = []
    cur_key = None
    cur: PremiumMinute | None = None
    for m in minutes:
        w = m.ts + IST_D
        key = (w.date(), w.hour, w.minute // 5)
        if key != cur_key:
            if cur is not None:
                out.append(cur)
            start = m.ts - timedelta(minutes=w.minute % 5)
            cur = PremiumMinute(ts=start + timedelta(minutes=4), o=m.o, h=m.h, l=m.l, c=m.c)
            cur_key = key
        else:
            cur = PremiumMinute(ts=cur.ts, o=cur.o, h=max(cur.h, m.h), l=min(cur.l, m.l), c=m.c)
    if cur is not None:
        out.append(cur)
    return out


async def dump_one(sym: str, exp: date, strike: int, ot: str, params: UmpParams, bar5: bool,
                   cut_utc=None) -> dict:
    # ``cut_utc`` = the instant the TradingView export was taken: the daily /
    # weekly / level snapshot only compares like-for-like at the same cursor.
    life = await build_premium_life(sym, exp, strike, ot, cut_utc=cut_utc)
    if life is None or len(life.minutes) < 2:
        return {"strike": strike, "ot": ot, "error": "no premium data"}
    feed = fold_5m(life.minutes) if bar5 else life.minutes
    engine, candles = replay_ump(feed, params, life.official_close)
    f = engine.feeds
    h1_struct = detect_h1_reversals(
        f.h1_candles, params.structural.body_match_pts, params.institutional.price_floor_inr
    )
    return {
        "strike": strike, "ot": ot, "expiry": exp.isoformat(),
        "sessions": [d.isoformat() for d in life.session_dates],
        "minutes": len(life.minutes),
        "bar5": bar5,
        "cut_utc": cut_utc.isoformat() if cut_utc else None,
        "trigger_timeout_bars": params.entry.trigger_timeout_bars,
        "last_close": engine.last_close,
        "daily": [list(d) for d in f.daily],
        "weekly": list(f.weekly) if f.weekly else None,
        "h1_candles": len(f.h1_candles),
        "h1_struct": h1_struct,
        "levels": [[lv.type, round(lv.price, 4)] for lv in engine.levels],
        "events": [[e.ts, e.kind, round(e.price, 4), e.text] for e in engine.events],
        "trades": [
            [t.entry_ts, round(t.entry_price, 4), t.sub_scenario, t.exit_ts, t.exit_price, t.exit_reason]
            for t in engine.trades
        ],
        "state": engine.state,
        "candles_5m": len(candles),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--expiry", required=True)
    ap.add_argument("--strikes", required=True, help="comma-separated")
    ap.add_argument("--ots", default="CE,PE")
    ap.add_argument("--bar5", action="store_true")
    ap.add_argument("--timeout-bars", type=int, default=None)
    ap.add_argument("--cut", default=None,
                    help="IST cursor 'YYYY-MM-DDTHH:MM' = when the TV export was taken "
                         "(the daily/weekly/level snapshot is compared at that instant)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    cut_utc = None
    if args.cut:
        from datetime import datetime as _dt

        from app.core.time_utils import ist_naive_to_utc

        cut_utc = ist_naive_to_utc(_dt.fromisoformat(args.cut))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    exp = date.fromisoformat(args.expiry)
    params = UmpParams()
    if args.timeout_bars is not None:
        params.entry.trigger_timeout_bars = int(args.timeout_bars)
    for k in (int(s) for s in args.strikes.split(",")):
        for ot in args.ots.split(","):
            r = await dump_one(args.symbol, exp, k, ot, params, args.bar5, cut_utc)
            (out / f"{args.symbol}_{exp.isoformat()}_{k}{ot}.json").write_text(
                json.dumps(r, default=str, indent=1), encoding="utf-8"
            )
            print(k, ot, r.get("error") or
                  f"min={r['minutes']} d={len(r['sessions'])} h1={len(r['h1_struct'])} "
                  f"lv={len(r['levels'])} ev={len(r['events'])} tr={len(r['trades'])}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
