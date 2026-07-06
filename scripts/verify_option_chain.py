"""Standalone script: compare XTS option chain data vs NSE India option chain.

Usage:
    python scripts/verify_option_chain.py [--symbol NIFTY] [--expiry YYYY-MM-DD]

Reads our data directly from TimescaleDB (no running backend required) and
fetches NSE option chain from nseindia.com. Prints a tabular diff report.

Set DATABASE_URL in .env (or the environment) before running.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

# psycopg async cannot run on Windows' default ProactorEventLoop (same setup
# as backend/run.py and the validation harness).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402  (loads .env)
from app.market.symbols import get_registry  # noqa: E402
from app.services.nse_option_chain import fetch_nse_option_chain  # noqa: E402


def _lot_size(symbol: str) -> int:
    try:
        entry = get_registry().get(symbol.upper())
        if entry is not None and getattr(entry, "lot_size", 0):
            return int(entry.lot_size)
    except Exception:
        pass
    return int(settings.nifty_lot_size or 1)

LTP_TOL_PTS = 5.0
LTP_TOL_PCT = 10.0
OI_TOL_PCT = 10.0

_LATEST_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry
    ORDER BY strike, option_type, ts DESC
    """
)


def _ltp_ok(ours: float | None, nse: float | None) -> tuple[float | None, bool]:
    if ours is None or nse is None:
        return None, False
    diff = ours - nse
    tol = max(LTP_TOL_PTS, abs(nse) * LTP_TOL_PCT / 100.0)
    return diff, abs(diff) <= tol


def _oi_ok(ours: int | None, nse: int | None) -> tuple[float | None, bool]:
    if ours is None or nse is None:
        return None, False
    pct = abs(ours - nse) / max(nse, 1) * 100.0
    return pct, pct <= OI_TOL_PCT


def _fmt(v: float | None, decimals: int = 1) -> str:
    return f"{v:.{decimals}f}" if v is not None else "—"


async def run(symbol: str, expiry_date: date) -> None:
    engine = create_async_engine(settings.db_url, echo=False)

    async with AsyncSession(engine) as sess:
        db_rows = (
            await sess.execute(_LATEST_SQL, {"symbol": symbol.upper(), "expiry": expiry_date})
        ).mappings().all()

    await engine.dispose()

    our_book: dict[int, dict[str, dict]] = {}
    for r in db_rows:
        strike = int(r["strike"])
        otype = str(r["option_type"])
        our_book.setdefault(strike, {})[otype] = {
            "ltp": float(r["ltp"]) if r.get("ltp") is not None else None,
            "oi": int(r["oi"]) if r.get("oi") is not None else None,
        }

    print(f"\nFetching NSE option chain for {symbol.upper()} expiry {expiry_date} …")
    nse_rows, nse_source = await fetch_nse_option_chain(symbol, expiry_filter=expiry_date)
    # NSE reports OI in CONTRACTS (lots); ours is units (contracts x lot size).
    lot = _lot_size(symbol)
    nse_book = {
        r.strike: replace(
            r,
            ce_oi=r.ce_oi * lot if r.ce_oi is not None else None,
            pe_oi=r.pe_oi * lot if r.pe_oi is not None else None,
        )
        for r in nse_rows
    }
    print(f"NSE OI scaled to units with lot size {lot}.")

    all_strikes = sorted(our_book.keys() | nse_book.keys())
    if not all_strikes:
        print("No data found in DB or NSE for the given symbol/expiry.")
        return

    header = (
        f"{'Strike':>8}  "
        f"{'CE LTP(us)':>10} {'CE LTP(nse)':>11} {'CE Δ':>7} {'CE_L':>4}  "
        f"{'CE OI(us)':>9} {'CE OI(nse)':>10} {'CE OI%':>7} {'CE_O':>4}  "
        f"{'PE LTP(us)':>10} {'PE LTP(nse)':>11} {'PE Δ':>7} {'PE_L':>4}  "
        f"{'PE OI(us)':>9} {'PE OI(nse)':>10} {'PE OI%':>7} {'PE_O':>4}"
    )
    print(f"\nNSE source : {nse_source}")
    print(f"DB rows    : {sum(len(v) for v in our_book.values())}")
    print(f"NSE rows   : {len(nse_rows)}")
    print(f"Expiry     : {expiry_date}")
    print(f"Tolerances : LTP ±{LTP_TOL_PTS}pts or ±{LTP_TOL_PCT}%, OI ±{OI_TOL_PCT}%\n")
    print(header)
    print("-" * len(header))

    ce_ltp_ok = pe_ltp_ok = ce_oi_ok = pe_oi_ok = 0
    missing_ours = missing_nse = 0

    for strike in all_strikes:
        in_ours = strike in our_book
        in_nse = strike in nse_book
        if not in_ours:
            missing_ours += 1
        if not in_nse:
            missing_nse += 1

        our_ce = our_book.get(strike, {}).get("CE", {})
        our_pe = our_book.get(strike, {}).get("PE", {})
        nse = nse_book.get(strike)

        ce_ld, ce_la = _ltp_ok(our_ce.get("ltp"), nse.ce_ltp if nse else None)
        pe_ld, pe_la = _ltp_ok(our_pe.get("ltp"), nse.pe_ltp if nse else None)
        ce_od, ce_oa = _oi_ok(our_ce.get("oi"), nse.ce_oi if nse else None)
        pe_od, pe_oa = _oi_ok(our_pe.get("oi"), nse.pe_oi if nse else None)

        if ce_la:
            ce_ltp_ok += 1
        if pe_la:
            pe_ltp_ok += 1
        if ce_oa:
            ce_oi_ok += 1
        if pe_oa:
            pe_oi_ok += 1

        ok = lambda b: "✓" if b else "✗"  # noqa: E731
        nse_ce_ltp = nse.ce_ltp if nse else None
        nse_pe_ltp = nse.pe_ltp if nse else None
        nse_ce_oi = nse.ce_oi if nse else None
        nse_pe_oi = nse.pe_oi if nse else None

        print(
            f"{strike:>8}  "
            f"{_fmt(our_ce.get('ltp')):>10} {_fmt(nse_ce_ltp):>11} {_fmt(ce_ld):>7} {ok(ce_la):>4}  "
            f"{_fmt(our_ce.get('oi'), 0):>9} {_fmt(nse_ce_oi, 0):>10} {_fmt(ce_od):>7} {ok(ce_oa):>4}  "
            f"{_fmt(our_pe.get('ltp')):>10} {_fmt(nse_pe_ltp):>11} {_fmt(pe_ld):>7} {ok(pe_la):>4}  "
            f"{_fmt(our_pe.get('oi'), 0):>9} {_fmt(nse_pe_oi, 0):>10} {_fmt(pe_od):>7} {ok(pe_oa):>4}"
        )

    n = len(all_strikes)
    print(f"\nSummary ({n} strikes):")
    print(f"  CE LTP aligned : {ce_ltp_ok}/{n}  ({ce_ltp_ok/n*100:.0f}%)" if n else "")
    print(f"  PE LTP aligned : {pe_ltp_ok}/{n}  ({pe_ltp_ok/n*100:.0f}%)" if n else "")
    print(f"  CE OI  aligned : {ce_oi_ok}/{n}  ({ce_oi_ok/n*100:.0f}%)" if n else "")
    print(f"  PE OI  aligned : {pe_oi_ok}/{n}  ({pe_oi_ok/n*100:.0f}%)" if n else "")
    if missing_ours:
        print(f"  Missing in our DB  : {missing_ours} strike(s)")
    if missing_nse:
        print(f"  Missing in NSE     : {missing_nse} strike(s)")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cross-check XTS option chain vs NSE India.")
    p.add_argument("--symbol", default="NIFTY", help="Underlying symbol (default: NIFTY)")
    p.add_argument(
        "--expiry",
        default=None,
        help="Expiry date YYYY-MM-DD; defaults to earliest in DB",
    )
    return p.parse_args()


async def _resolve_expiry_from_db(symbol: str) -> date | None:
    """Return earliest expiry in DB for the symbol."""
    engine = create_async_engine(settings.db_url, echo=False)
    try:
        async with AsyncSession(engine) as sess:
            row = await sess.execute(
                text("SELECT MIN(expiry) FROM option_oi_snapshots WHERE symbol = :sym"),
                {"sym": symbol.upper()},
            )
            val = row.scalar()
        return val
    except Exception as e:
        print(f"DB lookup failed: {e}", file=sys.stderr)
        return None
    finally:
        await engine.dispose()


async def main() -> None:
    args = _parse_args()
    if args.expiry:
        try:
            expiry_date = date.fromisoformat(args.expiry)
        except ValueError:
            print(f"Invalid expiry date: {args.expiry!r} (expected YYYY-MM-DD)")
            sys.exit(1)
    else:
        expiry_date = await _resolve_expiry_from_db(args.symbol)
        if expiry_date is None:
            print(f"No data in DB for symbol {args.symbol!r}. Use --expiry to specify a date.")
            sys.exit(1)
        print(f"Using earliest DB expiry: {expiry_date}")

    await run(args.symbol, expiry_date)


if __name__ == "__main__":
    asyncio.run(main())
