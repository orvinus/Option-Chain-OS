"""NSE UDiFF settlement bhavcopy -> eod_bars (source='nse_settlement').

Fills the hole the TrueData bhavcopy leaves: contracts that are LISTED but have
not traded yet get no bhavcopy row at all, so a young strike has no daily or
weekly history and Ultra Master Pro's DISCOVERY/BRIDGE levels are built from
the wrong candles (TradingView plots the settlement price as a flat bar and
Pine reads exactly that series). See app/market_data/nse_settlement.py for the
reconstruction rule and the verification against the TradingView chart.

Rows land under a distinct ``source`` and a distinct token namespace, so the
exchange-official ``td_bhavcopy`` rows are never overwritten -- the reader in
app/algo/series.py prefers bhavcopy on any date present in both.

Usage
-----
    python scripts/nse_settlement_backfill.py --symbol NIFTY --days 45
    python scripts/nse_settlement_backfill.py --from 2026-08-01 --to 2026-09-11
    python scripts/nse_settlement_backfill.py --days 5 --force   # re-fetch

Idempotent: a date that already has rows for this symbol under this source is
skipped unless --force. There is no ledger to get stuck in a 'done' state.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = ROOT / "backend" if (ROOT / "backend" / "app").is_dir() else ROOT
sys.path.insert(0, str(_PKG_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.time_utils import IST  # noqa: E402
from app.market_data.nse_settlement import (  # noqa: E402
    SOURCE,
    NseSettlementError,
    fetch_settlement_day,
    new_client,
    warm_cookies,
    weekdays,
)

_UPSERT_SQL = text(
    """
    INSERT INTO eod_bars
        (trade_date, symbol, expiry, strike, option_type, token,
         open, high, low, close, volume, oi, source)
    VALUES
        (:trade_date, :symbol, :expiry, :strike, :option_type, :token,
         :open, :high, :low, :close, :volume, :oi, :source)
    ON CONFLICT (trade_date, token) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
        close = EXCLUDED.close, volume = EXCLUDED.volume, oi = EXCLUDED.oi
    """
)

_HAVE_SQL = text(
    """
    SELECT trade_date, count(*) FROM eod_bars
    WHERE symbol = :symbol AND source = :source
      AND trade_date BETWEEN :start AND :end
    GROUP BY trade_date
    """
)


async def run(args: argparse.Namespace) -> int:
    today = datetime.now(IST).date()
    if args.date_from:
        start = date.fromisoformat(args.date_from)
        end = date.fromisoformat(args.date_to) if args.date_to else today
    else:
        end = today
        start = end - timedelta(days=args.days)

    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    try:
        async with engine.connect() as c:
            have = {
                d: n
                for d, n in (
                    await c.execute(
                        _HAVE_SQL,
                        {"symbol": args.symbol, "source": SOURCE,
                         "start": start, "end": end},
                    )
                ).all()
            }

        days = [d for d in weekdays(start, end)]
        todo = [d for d in days if args.force or not have.get(d)]
        print(
            f"== nse-settlement {args.symbol} {start} -> {end}: "
            f"{len(days)} weekdays, {len(days) - len(todo)} already present, "
            f"{len(todo)} to fetch"
        )

        written = total_idle = 0
        missing: list[date] = []
        async with new_client(args.timeout) as client:
            await warm_cookies(client)
            for d in todo:
                try:
                    bars = await fetch_settlement_day(d, args.symbol, client=client)
                except NseSettlementError as e:
                    print(f"   {d}  ERROR {e}")
                    continue
                if bars is None:
                    missing.append(d)
                    continue
                if not bars:
                    print(f"   {d}  file present, no {args.symbol} rows")
                    continue
                rows = [
                    {
                        "trade_date": b.trade_date, "symbol": b.symbol,
                        "expiry": b.expiry, "strike": b.strike,
                        "option_type": b.option_type, "token": b.token,
                        "open": b.open, "high": b.high, "low": b.low,
                        "close": b.close, "volume": b.volume, "oi": b.oi,
                        "source": SOURCE,
                    }
                    for b in bars
                ]
                async with engine.begin() as c:
                    for i in range(0, len(rows), 5000):
                        await c.execute(_UPSERT_SQL, rows[i: i + 5000])
                idle = sum(1 for b in bars if not b.traded)
                written += len(rows)
                total_idle += idle
                print(f"   {d}  {len(rows):5d} rows  ({idle} listed-but-idle)")
                await asyncio.sleep(args.sleep)

        print(
            f"== done: {written} rows written, {total_idle} of them "
            f"settlement-only (no trades that day)"
        )
        if missing:
            print(
                f"   no file on {len(missing)} weekday(s) "
                f"(holiday or not yet published): "
                + ", ".join(str(d) for d in missing)
            )
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="NIFTY")
    p.add_argument("--days", type=int, default=45,
                   help="calendar days back from today (ignored with --from)")
    p.add_argument("--from", dest="date_from", default=None)
    p.add_argument("--to", dest="date_to", default=None)
    p.add_argument("--force", action="store_true",
                   help="re-fetch dates that already have rows")
    p.add_argument("--sleep", type=float, default=0.6,
                   help="seconds between archive requests")
    p.add_argument("--timeout", type=float, default=45.0)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
