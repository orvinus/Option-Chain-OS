"""TrueData 6-month 1-min backfill → oi_archive_bars (NIFTY + SENSEX).

Subcommands
-----------
probe     Run the go/no-go probes (P-A…P-F) BEFORE any bulk pull. Prints a
          PASS/FAIL report and golden samples; writes logs/backfill/probe_report.md.
          The expired-contract probe is the KILL CRITERION: if getbars refuses
          expired weeklies, the whole backfill is off — escalate to the vendor.
pull      Extract 1-min bars (index + futures + options chains, newest expiry
          first) into oi_archive_bars, checkpointed in oi_backfill_progress
          (safe to re-run: completed units are skipped).
pull-ticks  Tick-by-tick capture (LTP/vol/OI + bid/ask) for the live chains —
          vendor serves only the LAST 5 TRADING DAYS, so run this daily during
          the trial or the data is gone. → oi_archive_ticks
pull-eod  Daily OHLCV+OI: 10+ years of index/futures dailies + NSE F&O
          bhavcopy days filtered to the underlying. → eod_bars
pull-flows  FII/DII daily flows (corporate host; skips gracefully if the
          account lacks the entitlement). → fii_dii_flows
pull-news  News archive, 4 publishers (corporate host; graceful skip). → news_items
validate  Post-pull sanity report: per-expiry-day row counts, minute-gap census,
          OI plausibility, ts range.

Typical 2-day runbook (run on the VPS so writes are DB-local):
    python scripts/truedata_backfill.py probe
    python scripts/truedata_backfill.py pull --symbol NIFTY
    python scripts/truedata_backfill.py pull --symbol SENSEX
    python scripts/truedata_backfill.py validate

Rules inherited from the vendor audit: CSV by header-name only, error strings
ride in 200 bodies, bearer dies ~04:00 IST (client auto-refreshes), OI units are
normalized via TRUEDATA_OI_SCALE_* config, and days already present in
option_oi_snapshots are SKIPPED so the read-side UNION view never double-counts.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

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
from app.market_data.truedata_rest import (  # noqa: E402
    QuotaExceeded,
    TrueDataError,
    TrueDataRest,
    month_chunks,
    parse_expiry_any,
    parse_option_tail,
)

OUT_DIR = ROOT / "logs" / "backfill"
SENTINEL_EXPIRY = date(1970, 1, 1)  # IDX / continuous-FUT rows
SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)

# Default TrueData index-bar symbols (overridable via --index-symbol; the probe
# prints the real names from getAllSymbols?segment=in).
INDEX_SYMBOLS = {"NIFTY": "NIFTY 50", "SENSEX": "SENSEX"}
EXCHANGE = {"NIFTY": "NSE", "SENSEX": "BSE"}

_UPSERT_SQL = text(
    """
    INSERT INTO oi_archive_bars
        (ts, symbol, expiry, strike, option_type, token,
         open, high, low, close, volume, volume_cum, oi, underlying)
    VALUES
        (:ts, :symbol, :expiry, :strike, :option_type, :token,
         :open, :high, :low, :close, :volume, :volume_cum, :oi, :underlying)
    ON CONFLICT (ts, token) DO UPDATE SET
        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
        close = EXCLUDED.close, volume = EXCLUDED.volume,
        volume_cum = EXCLUDED.volume_cum, oi = EXCLUDED.oi,
        underlying = COALESCE(EXCLUDED.underlying, oi_archive_bars.underlying)
    """
)
_PROGRESS_SQL = text(
    """
    INSERT INTO oi_backfill_progress (unit_key, status, rows, error, fetched_at)
    VALUES (:unit_key, :status, :rows, :error, now())
    ON CONFLICT (unit_key) DO UPDATE SET
        status = EXCLUDED.status, rows = EXCLUDED.rows,
        error = EXCLUDED.error, fetched_at = now()
    """
)
_UPSERT_TICKS_SQL = text(
    """
    INSERT INTO oi_archive_ticks
        (ts, symbol, expiry, strike, option_type, token,
         ltp, volume, oi, bid, bidqty, ask, askqty)
    VALUES
        (:ts, :symbol, :expiry, :strike, :option_type, :token,
         :ltp, :volume, :oi, :bid, :bidqty, :ask, :askqty)
    ON CONFLICT (ts, token) DO UPDATE SET
        ltp = EXCLUDED.ltp, volume = EXCLUDED.volume, oi = EXCLUDED.oi,
        bid = EXCLUDED.bid, bidqty = EXCLUDED.bidqty,
        ask = EXCLUDED.ask, askqty = EXCLUDED.askqty
    """
)
_UPSERT_EOD_SQL = text(
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
_UPSERT_FLOWS_SQL = text(
    """
    INSERT INTO fii_dii_flows (trade_date, category, exchange, buy, sell, net)
    VALUES (:trade_date, :category, :exchange, :buy, :sell, :net)
    ON CONFLICT (trade_date, category, exchange) DO UPDATE SET
        buy = EXCLUDED.buy, sell = EXCLUDED.sell, net = EXCLUDED.net
    """
)
_UPSERT_NEWS_SQL = text(
    """
    INSERT INTO news_items (id, pub_date, title, description, category, source, source_link)
    VALUES (:id, :pub_date, :title, :description, :category, :source, :source_link)
    ON CONFLICT (id) DO NOTHING
    """
)


def _oi_scale(symbol: str) -> int:
    return (
        settings.truedata_oi_scale_bse
        if EXCHANGE.get(symbol, "NSE") == "BSE"
        else settings.truedata_oi_scale_nse
    )


def _ist_naive_to_utc(s: str) -> datetime | None:
    """Vendor bar stamps are IST-naive ('2025-02-14T09:15:00' or with space)."""
    v = s.strip().replace(" ", "T")
    try:
        naive = datetime.fromisoformat(v)
    except ValueError:
        return None
    return naive.replace(tzinfo=IST).astimezone(timezone.utc)


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _i(v: str | None) -> int:
    try:
        return int(float(v)) if v not in (None, "") else 0
    except ValueError:
        return 0


def _bars_to_rows(
    bars: list[dict[str, str]],
    *,
    symbol: str,
    expiry: date,
    strike: int,
    option_type: str,
    token: str,
    spot_by_minute: dict[datetime, float] | None,
) -> list[dict]:
    """Transform vendor bar dicts → archive rows: IST→UTC, OI scale, per-day
    cumulative volume, spot stamping. Bars must be processed oldest→newest for
    the running volume sum, so sort defensively."""
    scale = _oi_scale(symbol)
    rows: list[dict] = []
    cum: dict[date, int] = defaultdict(int)
    parsed = []
    for b in bars:
        ts = _ist_naive_to_utc(b.get("timestamp") or "")
        if ts is None:
            continue
        parsed.append((ts, b))
    parsed.sort(key=lambda x: x[0])
    for ts, b in parsed:
        ist_day = ts.astimezone(IST).date()
        vol = _i(b.get("volume"))
        cum[ist_day] += vol
        spot = spot_by_minute.get(ts) if spot_by_minute else None
        rows.append(
            {
                "ts": ts,
                "symbol": symbol,
                "expiry": expiry,
                "strike": strike,
                "option_type": option_type,
                "token": token,
                "open": _f(b.get("open")),
                "high": _f(b.get("high")),
                "low": _f(b.get("low")),
                "close": _f(b.get("close")),
                "volume": vol,
                "volume_cum": cum[ist_day],
                "oi": _i(b.get("oi")) * scale,
                "underlying": spot,
            }
        )
    return rows


class Backfill:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.td = TrueDataRest(rps=args.rps)
        self.engine = create_async_engine(settings.db_url, pool_pre_ping=True)
        self.done_units: set[str] = set()
        self.live_days: dict[str, set[date]] = {}
        self.report_lines: list[str] = []

    async def close(self) -> None:
        await self.td.aclose()
        await self.engine.dispose()

    def note(self, line: str) -> None:
        print(line, flush=True)
        self.report_lines.append(line)

    # ---------------- shared DB helpers ----------------

    async def _load_done_units(self) -> None:
        async with self.engine.begin() as c:
            rows = await c.execute(
                text("SELECT unit_key FROM oi_backfill_progress WHERE status = 'done'")
            )
            self.done_units = {r[0] for r in rows}

    async def _load_live_days(self, symbol: str) -> set[date]:
        """IST days already recorded live — the importer skips them entirely so
        the UNION ALL view can never double-count.

        ``--include-live-days`` disables the skip for REPAIR pulls: the 18
        partial dev-machine recordings transferred as gap days (the Jun-10
        put-collapse / truncated-session classes in the data-quality report)
        can only be replaced by vendor data if the skip is bypassed. The
        no-double-count invariant then holds at TIMESTAMP level (upserts), not
        day level — callers must delete the inferior rows after the pull."""
        if getattr(self.args, "include_live_days", False):
            self.live_days[symbol] = set()
            return set()
        if symbol not in self.live_days:
            async with self.engine.begin() as c:
                rows = await c.execute(
                    text(
                        "SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date "
                        "FROM option_oi_snapshots WHERE symbol = :s"
                    ),
                    {"s": symbol},
                )
                self.live_days[symbol] = {r[0] for r in rows}
        return self.live_days[symbol]

    async def _write(self, rows: list[dict], symbol: str) -> int:
        if not rows:
            return 0
        live = await self._load_live_days(symbol)
        rows = [r for r in rows if r["ts"].astimezone(IST).date() not in live]
        if not rows:
            return 0
        async with self.engine.begin() as c:
            for i in range(0, len(rows), 5000):
                await c.execute(_UPSERT_SQL, rows[i : i + 5000])
        return len(rows)

    async def _mark(self, unit_key: str, status: str, rows: int = 0, error: str | None = None) -> None:
        async with self.engine.begin() as c:
            await c.execute(
                _PROGRESS_SQL,
                {"unit_key": unit_key, "status": status, "rows": rows, "error": (error or "")[:500]},
            )

    # ---------------- discovery ----------------

    def _window(self) -> tuple[datetime, datetime]:
        end = datetime.now(IST).replace(tzinfo=None)
        start = (end - timedelta(days=self.args.months * 31)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        return start, end

    async def _discover_expiries(self, symbol: str) -> list[date]:
        """Past + near-future expiries for the window.

        Measured vendor reality (probe 2026-08-10): ``getSymbolExpiryList``
        returns FUTURE expiries only. Past weeklies are therefore *generated*
        from the weekly weekday pattern of the nearest future expiries (NIFTY =
        Tuesday, SENSEX = Thursday); a holiday-shifted expiry is caught later by
        the pull's E−1/E−2 contract-discovery fallback. Verified that
        ``getSymbolOptionChain`` serves past expiries (113 contracts for the
        expired 2026-08-04 NIFTY weekly)."""
        raw = await self.td.get_symbol_expiry_list(symbol)
        today = datetime.now(IST).date()
        start, _end = self._window()
        future = sorted(e for e in (parse_expiry_any(v) for v in raw) if e and e >= today)
        if not future:
            self.note(f"!! {symbol}: expiry list empty/unparseable — raw sample: {raw[:5]}")
            return []
        # Weekly weekday = the most common weekday among the nearest 4 expiries.
        weekdays = [e.weekday() for e in future[:4]]
        wk = max(set(weekdays), key=weekdays.count)
        past: list[date] = []
        d = today - timedelta(days=1)
        while d >= start.date():
            if d.weekday() == wk:
                past.append(d)
            d -= timedelta(days=1)
        near_future = [e for e in future if e <= today + timedelta(days=60)]
        # newest first — highest value lands first if interrupted
        return sorted(set(near_future) | set(past), reverse=True)

    async def _discover_contracts(self, symbol: str, expiry: date) -> list[tuple[str, int, str]]:
        """→ [(contract_symbol, strike, 'CE'|'PE')]. Strike/type come from the
        symbol TAIL, so this works regardless of how the (undocumented) naming
        encodes the expiry in the middle."""
        rows = await self.td.get_symbol_option_chain(symbol, expiry.strftime("%y%m%d"))
        out: list[tuple[str, int, str]] = []
        seen: set[str] = set()
        for r in rows:
            for v in r.values():
                v = (v or "").strip()
                if not v or v in seen:
                    continue
                tail = parse_option_tail(v)
                if tail and v.upper().startswith(symbol.upper()[:4]):
                    seen.add(v)
                    out.append((v, int(round(tail[0])), tail[1]))
        return out

    # ---------------- index series ----------------

    async def _pull_index(self, symbol: str) -> dict[datetime, float]:
        """Fetch the index 1-min series (spot context), store IDX rows, and
        return the minute→close map used to stamp `underlying`.

        The map is ALWAYS completed from the DB afterwards: on a resumed run the
        chunk units are 'done' and skipped, and without the DB read the map came
        back empty — every contract fetched after a resume got underlying=NULL
        (found the hard way in the first pull)."""
        idx_symbol = self.args.index_symbol or INDEX_SYMBOLS.get(symbol, symbol)
        start, end = self._window()
        spot: dict[datetime, float] = {}
        for c_start, c_end in reversed(month_chunks(start, end)):
            unit = f"{symbol}:IDX:{c_start:%y%m%d}"
            bars = await self._fetch_unit(unit, idx_symbol, c_start, c_end)
            if bars is None:
                continue
            rows = _bars_to_rows(
                bars, symbol=symbol, expiry=SENTINEL_EXPIRY, strike=0,
                option_type="IDX", token=f"td:{symbol}:IDX", spot_by_minute=None,
            )
            for r in rows:
                if r["close"] is not None:
                    spot[r["ts"]] = r["close"]
                r["underlying"] = r["close"]
            n = await self._write(rows, symbol)
            await self._mark(unit, "done", n)
        # Complete the map from everything already stored (skipped chunks, prior runs).
        async with self.engine.begin() as c:
            db_rows = await c.execute(
                text(
                    "SELECT ts, close FROM oi_archive_bars "
                    "WHERE symbol = :s AND option_type = 'IDX' AND close IS NOT NULL"
                ),
                {"s": symbol},
            )
            for ts, close in db_rows:
                spot.setdefault(ts, float(close))
        self.note(f"   {symbol}: index series minutes={len(spot)}")
        return spot

    async def _pull_futures(self, symbol: str) -> None:
        """Continuous-future 1-min series (``SYMBOL-I``) as FUT context rows.
        Best-effort: continuous-symbol semantics are vendor-side; an empty
        response just logs and moves on."""
        start, end = self._window()
        total = 0
        for c_start, c_end in reversed(month_chunks(start, end)):
            unit = f"{symbol}:FUT:{c_start:%y%m%d}"
            bars = await self._fetch_unit(unit, f"{symbol}-I", c_start, c_end)
            if bars is None:
                continue
            rows = _bars_to_rows(
                bars, symbol=symbol, expiry=SENTINEL_EXPIRY, strike=0,
                option_type="FUT", token=f"td:{symbol}:FUT-I", spot_by_minute=None,
            )
            n = await self._write(rows, symbol)
            await self._mark(unit, "done", n)
            total += n
        self.note(f"   {symbol}: futures ({symbol}-I) rows={total}"
                  + ("" if total else " — continuous symbol may not be served; not fatal"))

    async def _fetch_unit(
        self, unit: str, fetch_symbol: str, start: datetime, end: datetime
    ) -> list[dict[str, str]] | None:
        """One governed getbars call with quota backoff; None ⇒ already done."""
        if unit in self.done_units:
            return None
        for attempt in range(5):
            try:
                return await self.td.get_bars(fetch_symbol, start, end, "1min")
            except QuotaExceeded:
                wait = min(60.0, 2.0 * (2 ** attempt))
                print(f"   quota hit on {unit}; backing off {wait:.0f}s", flush=True)
                await asyncio.sleep(wait)
            except TrueDataError as e:
                await self._mark(unit, "error", 0, str(e))
                print(f"   !! {unit}: {e}", flush=True)
                return []
        await self._mark(unit, "error", 0, "quota backoff exhausted")
        return []

    # ---------------- pull ----------------

    async def pull(self) -> None:
        symbol = self.args.symbol.upper()
        await self._load_done_units()
        await self._load_live_days(symbol)
        self.note(f"== pull {symbol} — window {self.args.months} months, "
                  f"rps={self.td.gov.min_interval and round(1 / self.td.gov.min_interval, 1)}, "
                  f"live-days skipped={len(self.live_days[symbol])}")

        spot = await self._pull_index(symbol)
        await self._pull_futures(symbol)

        expiries = await self._discover_expiries(symbol)
        self.note(f"   expiries in window: {len(expiries)} ({expiries[:3]}…{expiries[-1:] if expiries else ''})")
        start, end = self._window()

        total_rows = 0
        for expiry in expiries:
            contracts = await self._discover_contracts(symbol, expiry)
            # Holiday-shifted weekly: the generated weekday date has no chain;
            # the real (shifted-earlier) expiry is 1–2 days before it.
            if not contracts:
                for shift in (1, 2):
                    shifted = expiry - timedelta(days=shift)
                    contracts = await self._discover_contracts(symbol, shifted)
                    if contracts:
                        expiry = shifted
                        break
            if not contracts:
                self.note(f"   !! {symbol} {expiry}: no contracts (also tried −1/−2 days) — "
                          f"holiday week or pre-listing; skipping")
                continue
            # A weekly trades ~2 weeks before expiry; monthlies longer. Cap the
            # request span to the contract's plausible life to avoid burning
            # quota on empty months.
            c_start = max(start, datetime.combine(expiry - timedelta(days=45), SESSION_OPEN))
            c_end = min(end, datetime.combine(expiry + timedelta(days=1), SESSION_CLOSE))
            if c_start >= c_end:
                continue
            sem = asyncio.Semaphore(self.args.workers)
            rows_written = 0

            async def one(contract: str, strike: int, opt: str) -> int:
                nonlocal rows_written
                unit = f"{symbol}:{expiry:%y%m%d}:{strike}:{opt}"
                async with sem:
                    if self.args.dry_run and rows_written:
                        return 0
                    bars = await self._fetch_unit(unit, contract, c_start, c_end)
                    if bars is None:
                        return 0
                    rows = _bars_to_rows(
                        bars, symbol=symbol, expiry=expiry, strike=strike,
                        option_type=opt, token=f"td:{symbol}:{expiry:%y%m%d}:{strike}:{opt}",
                        spot_by_minute=spot,
                    )
                    n = await self._write(rows, symbol)
                    await self._mark(unit, "done", n)
                    rows_written += n
                    return n

            results = await asyncio.gather(*(one(c, k, o) for c, k, o in contracts))
            total_rows += sum(results)
            self.note(f"   {symbol} {expiry}: contracts={len(contracts)} rows+={sum(results)}")
            if self.args.dry_run:
                self.note("   (dry-run: stopping after first expiry)")
                break
        self.note(f"== pull {symbol} complete: rows written {total_rows}")
        self._write_report(f"pull_{symbol.lower()}")

    # ---------------- pull-ticks (last 5 trading days — use it or lose it) ----------------

    async def pull_ticks(self) -> None:
        symbol = self.args.symbol.upper()
        await self._load_done_units()
        today = datetime.now(IST).date()
        days = self.args.days or 6
        start = datetime.combine(today - timedelta(days=days), SESSION_OPEN)
        end = datetime.combine(today, SESSION_CLOSE)
        self.note(f"== pull-ticks {symbol} — {start.date()} → {end.date()} "
                  f"(vendor serves ~5 trading days max)")

        # Contracts: expiries near today (live + just-expired) — those are the
        # chains that actually traded in the tick window. _discover_expiries
        # generates past weeklies too (the vendor lists only future dates).
        all_exp = await self._discover_expiries(symbol)
        near = sorted(
            e for e in all_exp
            if today - timedelta(days=days + 3) <= e <= today + timedelta(days=45)
        )
        self.note(f"   expiries near window: {near}")

        idx_symbol = self.args.index_symbol or INDEX_SYMBOLS.get(symbol, symbol)
        targets: list[tuple[str, date, int, str, str]] = [
            (idx_symbol, SENTINEL_EXPIRY, 0, "IDX", f"td:{symbol}:IDX"),
            (f"{symbol}-I", SENTINEL_EXPIRY, 0, "FUT", f"td:{symbol}:FUT-I"),
        ]
        for expiry in near:
            for contract, strike, opt in await self._discover_contracts(symbol, expiry):
                targets.append(
                    (contract, expiry, strike, opt, f"td:{symbol}:{expiry:%y%m%d}:{strike}:{opt}")
                )
        self.note(f"   tick targets: {len(targets)}")

        scale = _oi_scale(symbol)
        sem = asyncio.Semaphore(self.args.workers)
        total = 0

        async def one(fetch_symbol: str, expiry: date, strike: int, opt: str, token: str) -> int:
            unit = f"ticks:{token}:{start:%y%m%d}"
            if unit in self.done_units:
                return 0
            async with sem:
                try:
                    ticks = await self.td.get_ticks(fetch_symbol, start, end, bidask=True)
                except QuotaExceeded:
                    await asyncio.sleep(10)
                    ticks = await self.td.get_ticks(fetch_symbol, start, end, bidask=True)
                except TrueDataError:
                    # bid/ask history may be a missing entitlement — degrade once.
                    try:
                        ticks = await self.td.get_ticks(fetch_symbol, start, end, bidask=False)
                    except TrueDataError as e2:
                        await self._mark(unit, "error", 0, str(e2))
                        return 0
                rows = []
                for t in ticks:
                    ts = _ist_naive_to_utc(t.get("timestamp") or "")
                    if ts is None:
                        continue
                    rows.append(
                        {
                            "ts": ts, "symbol": symbol, "expiry": expiry,
                            "strike": strike, "option_type": opt, "token": token,
                            "ltp": _f(t.get("ltp")), "volume": _i(t.get("volume")),
                            "oi": _i(t.get("oi")) * scale,
                            "bid": _f(t.get("bid")), "bidqty": _i(t.get("bidqty")) or None,
                            "ask": _f(t.get("ask")), "askqty": _i(t.get("askqty")) or None,
                        }
                    )
                async with self.engine.begin() as c:
                    for i in range(0, len(rows), 5000):
                        await c.execute(_UPSERT_TICKS_SQL, rows[i : i + 5000])
                await self._mark(unit, "done", len(rows))
                return len(rows)

        results = await asyncio.gather(*(one(*t) for t in targets))
        total = sum(results)
        self.note(f"== pull-ticks {symbol} complete: rows {total}")
        self._write_report(f"ticks_{symbol.lower()}")

    # ---------------- pull-eod (daily OHLCV+OI, 10+ years for index/futures) ----------------

    async def pull_eod(self) -> None:
        symbol = self.args.symbol.upper()
        await self._load_done_units()
        today = datetime.now(IST).date()
        days = self.args.days or 130
        self.note(f"== pull-eod {symbol} — index/futures dailies {self.args.years}y "
                  f"+ NSE F&O bhavcopy {days}d")

        # (a) long-horizon daily series: index + continuous future.
        idx_symbol = self.args.index_symbol or INDEX_SYMBOLS.get(symbol, symbol)
        for fetch_symbol, opt, token in (
            (idx_symbol, "IDX", f"td:{symbol}:IDX:EOD"),
            (f"{symbol}-I", "FUT", f"td:{symbol}:FUT-I:EOD"),
        ):
            start = datetime.combine(today - timedelta(days=self.args.years * 365), SESSION_OPEN)
            end = datetime.combine(today, SESSION_CLOSE)
            unit = f"eod:{token}"
            if unit in self.done_units:
                continue
            try:
                bars = await self.td.get_bars(fetch_symbol, start, end, "eod")
            except TrueDataError as e:
                await self._mark(unit, "error", 0, str(e))
                self.note(f"   !! {fetch_symbol} eod: {e}")
                continue
            rows = []
            for b in bars:
                ts = _ist_naive_to_utc(b.get("timestamp") or "")
                if ts is None:
                    continue
                rows.append(
                    {
                        "trade_date": ts.astimezone(IST).date(), "symbol": symbol,
                        "expiry": SENTINEL_EXPIRY, "strike": 0, "option_type": opt,
                        "token": token, "open": _f(b.get("open")), "high": _f(b.get("high")),
                        "low": _f(b.get("low")), "close": _f(b.get("close")),
                        "volume": _i(b.get("volume")), "oi": _i(b.get("oi")) * _oi_scale(symbol),
                        "source": "td_eod",
                    }
                )
            async with self.engine.begin() as c:
                for i in range(0, len(rows), 5000):
                    await c.execute(_UPSERT_EOD_SQL, rows[i : i + 5000])
            await self._mark(unit, "done", len(rows))
            self.note(f"   {fetch_symbol} eod: {len(rows)} days")

        # (b) contract-level EOD via official bhavcopy — NSE FO only (a BSE
        # bhavcopy segment is not documented; SENSEX contract EOD can be rolled
        # up from the 1-min archive instead).
        if EXCHANGE.get(symbol) == "NSE":
            day = today
            fetched = 0
            while fetched < days:
                day -= timedelta(days=1)
                if day.weekday() >= 5:
                    continue
                fetched += 1
                unit = f"bhav:fo:{day:%y%m%d}"
                if unit in self.done_units:
                    continue
                try:
                    rows_raw = await self.td.get_bhavcopy("fo", day)
                except QuotaExceeded:
                    await asyncio.sleep(10)
                    continue
                except TrueDataError as e:
                    await self._mark(unit, "error", 0, str(e))
                    continue
                rows = []
                for r in rows_raw:
                    vsym = (r.get("symbol") or "").strip()
                    # Prefix match excludes BANKNIFTY/FINNIFTY/MIDCPNIFTY (they
                    # don't START with 'NIFTY') — and the char after the prefix
                    # must be a digit, which excludes NIFTYNXT50 (found the hard
                    # way: its '50' polluted the strike parse in the first run).
                    rest = vsym.upper()[len(symbol):]
                    if not vsym.upper().startswith(symbol) or not rest[:1].isdigit():
                        continue
                    tail = parse_option_tail(vsym)
                    strike, opt = (int(round(tail[0])), tail[1]) if tail else (
                        0, "FUT" if vsym.upper().endswith(("FUT", "-I")) else "EQ"
                    )
                    rows.append(
                        {
                            "trade_date": day, "symbol": symbol, "expiry": SENTINEL_EXPIRY,
                            "strike": strike, "option_type": opt, "token": f"td:bhav:{vsym}",
                            "open": _f(r.get("open")), "high": _f(r.get("high")),
                            "low": _f(r.get("low")), "close": _f(r.get("close")),
                            "volume": _i(r.get("volume")), "oi": _i(r.get("oi")) * _oi_scale(symbol),
                            "source": "td_bhavcopy",
                        }
                    )
                if rows:
                    async with self.engine.begin() as c:
                        for i in range(0, len(rows), 5000):
                            await c.execute(_UPSERT_EOD_SQL, rows[i : i + 5000])
                await self._mark(unit, "done", len(rows))
            self.note(f"   bhavcopy fo: {fetched} trading days attempted")
        self._write_report(f"eod_{symbol.lower()}")

    # ---------------- pull-flows (FII/DII) & pull-news (corporate host) ----------------

    async def pull_flows(self) -> None:
        await self._load_done_units()
        today = datetime.now(IST).date()
        days = self.args.days or 190
        self.note(f"== pull-flows — FII/DII last {days} days (corporate host, 1/s)")
        errors_in_a_row = 0
        written = 0
        for n in range(1, days + 1):
            day = today - timedelta(days=n)
            if day.weekday() >= 5:
                continue
            unit = f"fii:{day:%y%m%d}"
            if unit in self.done_units:
                continue
            try:
                rows_raw = await self.td.get_fii_dii(day)
                errors_in_a_row = 0
            except TrueDataError as e:
                errors_in_a_row += 1
                await self._mark(unit, "error", 0, str(e))
                if errors_in_a_row >= 5:
                    self.note(f"   !! 5 consecutive errors ({e}) — corporate host likely "
                              "not entitled on this account; stopping flows")
                    break
                continue
            rows = [
                {
                    "trade_date": day, "category": (r.get("category") or "?").strip(),
                    "exchange": (r.get("exchange") or "NSE").strip(),
                    "buy": _f(r.get("buy")), "sell": _f(r.get("sell")), "net": _f(r.get("net")),
                }
                for r in rows_raw if r.get("category")
            ]
            if rows:
                async with self.engine.begin() as c:
                    await c.execute(_UPSERT_FLOWS_SQL, rows)
                written += len(rows)
            await self._mark(unit, "done", len(rows))
        self.note(f"== pull-flows complete: rows {written}")
        self._write_report("flows")

    async def pull_news(self) -> None:
        await self._load_done_units()
        now = datetime.now(IST).replace(tzinfo=None)
        days = self.args.days or 190
        self.note(f"== pull-news — last {days} days (corporate host)")
        errors_in_a_row = 0
        written = 0
        cur = now - timedelta(days=days)
        while cur < now:
            nxt = min(cur + timedelta(days=5), now)
            unit = f"news:{cur:%y%m%d}"
            if unit not in self.done_units:
                try:
                    rows_raw = await self.td.get_news(cur, nxt, top=1000)
                    errors_in_a_row = 0
                except TrueDataError as e:
                    errors_in_a_row += 1
                    await self._mark(unit, "error", 0, str(e))
                    if errors_in_a_row >= 3:
                        self.note(f"   !! repeated errors ({e}) — News API likely not "
                                  "entitled; stopping")
                        break
                    cur = nxt
                    continue
                rows = []
                for r in rows_raw:
                    rid = (r.get("id") or "").strip()
                    if not rid:
                        continue
                    pub = None
                    try:
                        pub = datetime.fromisoformat((r.get("pub_date") or "").strip()).replace(tzinfo=IST)
                    except ValueError:
                        pass
                    rows.append(
                        {
                            "id": rid, "pub_date": pub, "title": r.get("title"),
                            "description": r.get("description"), "category": r.get("category"),
                            "source": r.get("source"), "source_link": r.get("source_link"),
                        }
                    )
                if rows:
                    async with self.engine.begin() as c:
                        await c.execute(_UPSERT_NEWS_SQL, rows)
                    written += len(rows)
                await self._mark(unit, "done", len(rows))
            cur = nxt
        self.note(f"== pull-news complete: rows {written}")
        self._write_report("news")

    # ---------------- probe ----------------

    async def probe(self) -> None:
        self.note("== TrueData backfill probes ==")
        ok = True

        # P-A auth (+ implicit REST reachability)
        try:
            await self.td._bearer()  # noqa: SLF001 — deliberate: auth-only probe
            self.note("P-A auth: PASS (bearer issued)")
        except Exception as e:
            self.note(f"P-A auth: FAIL — {e}")
            self._write_report("probe")
            raise SystemExit(2)

        # Index master names (segment=in) — verifies entitlement + real names
        try:
            master = await self.td.get_all_symbols("in", search="NIFTY")
            self.note(f"P-A2 index master head:\n{master[:400]}")
        except Exception as e:
            self.note(f"P-A2 index master: WARN — {e}")

        # P-B golden sample: nearest upcoming NIFTY expiry, ATM-ish contract, 2 days
        try:
            expiries_all = [parse_expiry_any(v) for v in await self.td.get_symbol_expiry_list("NIFTY")]
            today = datetime.now(IST).date()
            upcoming = sorted(e for e in expiries_all if e and e >= today)
            past = sorted((e for e in expiries_all if e and e < today), reverse=True)
            self.note(f"P-B expiry list: {len(expiries_all)} parsed; nearest upcoming={upcoming[:1]}, "
                      f"most recent past={past[:3]}")
            probe_exp = upcoming[0] if upcoming else past[0]
            contracts = await self._discover_contracts("NIFTY", probe_exp)
            self.note(f"P-B chain {probe_exp}: {len(contracts)} contracts; sample={[c[0] for c in contracts[:4]]}")
            if contracts:
                mid = contracts[len(contracts) // 2]
                s = datetime.combine(today - timedelta(days=3), SESSION_OPEN)
                e = datetime.combine(today, SESSION_CLOSE)
                bars = await self.td.get_bars(mid[0], s, e, "1min")
                if bars:
                    keys = list(bars[0].keys())
                    first = bars[0]
                    self.note(f"P-B bars: PASS — {len(bars)} bars, header={keys}, first={first}")
                    if "oi" not in keys or "volume" not in keys:
                        ok = False
                        self.note("P-B: FAIL — oi/volume columns missing after aliasing; DO NOT PULL")
                    hhmm = (first.get("timestamp") or "")[-8:-3]
                    self.note(f"P-B first-bar label: {first.get('timestamp')} "
                              f"(expect 09:15 bucket-open; if 09:16 labels are bucket-CLOSE — adjust!)")
                    oi_vals = [int(float(b.get("oi") or 0)) for b in bars[:500]]
                    nz = [v for v in oi_vals if v]
                    if nz:
                        lot65 = sum(1 for v in nz if v % 65 == 0) / len(nz)
                        lot1 = sum(1 for v in nz if v < 1_000_000) / len(nz)
                        self.note(f"P-B OI units heuristic: %divisible-by-65={lot65:.0%}, "
                                  f"%under-1M={lot1:.0%} → high divisibility ⇒ UNITS (scale=1); "
                                  f"small magnitudes ⇒ LOTS (set TRUEDATA_OI_SCALE_NSE=65)")
                else:
                    ok = False
                    self.note("P-B bars: FAIL — empty response for a live contract")
        except Exception as e:
            ok = False
            self.note(f"P-B: FAIL — {e}")

        # P-C expired-contract test — THE KILL CRITERION. The vendor lists only
        # future expiries, so take a *generated* past weekly (≥1 week old).
        try:
            all_exp = await self._discover_expiries("NIFTY")
            past = [e for e in all_exp if e < datetime.now(IST).date() - timedelta(days=6)]
            probe_exp = past[0]
            contracts = await self._discover_contracts("NIFTY", probe_exp)
            if not contracts:
                ok = False
                self.note(f"P-C expired chain {probe_exp}: FAIL — no contracts returned. KILL CRITERION.")
            else:
                mid = contracts[len(contracts) // 2]
                s = datetime.combine(probe_exp - timedelta(days=5), SESSION_OPEN)
                e = datetime.combine(probe_exp, SESSION_CLOSE)
                bars = await self.td.get_bars(mid[0], s, e, "1min")
                if bars:
                    self.note(f"P-C expired bars ({mid[0]}, exp {probe_exp}): PASS — {len(bars)} bars")
                else:
                    ok = False
                    self.note(f"P-C expired bars ({mid[0]}): FAIL — empty. KILL CRITERION: "
                              "getbars does not serve expired contracts; STOP and escalate to vendor.")
        except Exception as e:
            ok = False
            self.note(f"P-C: FAIL — {e}. KILL CRITERION if persistent.")

        # P-D depth: index bars ~6 months back
        try:
            idx = self.args.index_symbol or "NIFTY 50"
            old = datetime.now(IST).replace(tzinfo=None) - timedelta(days=self.args.months * 30)
            bars = await self.td.get_bars(idx, old.replace(hour=9, minute=15),
                                          old.replace(hour=15, minute=30), "1min")
            self.note(f"P-D depth ({idx} @ {old.date()}): {'PASS — ' + str(len(bars)) + ' bars' if bars else 'FAIL — empty (window shallower than requested?)'}")
        except Exception as e:
            self.note(f"P-D: WARN — {e}")

        # P-E SENSEX (BSE) golden sample — NEAREST upcoming expiry (the list
        # includes long-dated 2031 expiries with no contracts; never take max()).
        try:
            exp_raw = await self.td.get_symbol_expiry_list("SENSEX")
            today_d = datetime.now(IST).date()
            exps = sorted(e for e in (parse_expiry_any(v) for v in exp_raw) if e and e >= today_d)
            if not exps:
                self.note("P-E SENSEX: FAIL — empty expiry list (no bsefo entitlement?) → NIFTY-only fallback")
            else:
                contracts = await self._discover_contracts("SENSEX", exps[0])
                self.note(f"P-E SENSEX chain {exps[0]}: {len(contracts)} contracts "
                          f"{'PASS' if contracts else 'FAIL → NIFTY-only fallback'}")
        except Exception as e:
            self.note(f"P-E SENSEX: FAIL — {e} → NIFTY-only fallback")

        self.note(f"\n== probes {'ALL CLEAR — proceed to pull' if ok else 'BLOCKED — fix FAILs before pull'} ==")
        self._write_report("probe")
        if not ok:
            raise SystemExit(1)

    # ---------------- validate ----------------

    async def validate(self) -> None:
        async with self.engine.begin() as c:
            summary = (await c.execute(text(
                "SELECT symbol, option_type, count(*), min(ts), max(ts), "
                "count(DISTINCT expiry) FROM oi_archive_bars GROUP BY 1, 2 ORDER BY 1, 2"
            ))).all()
            self.note("== archive summary (symbol, type, rows, min_ts, max_ts, expiries)")
            for r in summary:
                self.note(f"   {r[0]:8s} {r[1]:3s} rows={r[2]:>10,} {r[3]} → {r[4]} expiries={r[5]}")

            gaps = (await c.execute(text(
                """
                WITH per_day AS (
                    SELECT symbol, (ts AT TIME ZONE 'Asia/Kolkata')::date AS d,
                           count(DISTINCT date_trunc('minute', ts)) AS minutes
                    FROM oi_archive_bars WHERE option_type IN ('CE','PE')
                    GROUP BY 1, 2
                )
                SELECT symbol, d, minutes FROM per_day
                WHERE minutes < 300 ORDER BY symbol, d
                """
            ))).all()
            self.note(f"== thin days (<300 distinct minutes of chain data): {len(gaps)}")
            for r in gaps[:20]:
                self.note(f"   {r[0]} {r[1]} minutes={r[2]}")

            oi_bad = (await c.execute(text(
                "SELECT symbol, count(*) FROM oi_archive_bars "
                "WHERE option_type IN ('CE','PE') AND (oi < 0 OR oi > 1000000000) GROUP BY 1"
            ))).all()
            self.note(f"== implausible OI rows: {oi_bad or 'none'}")

            strike_bad = (await c.execute(text(
                "SELECT symbol, count(*) FROM oi_archive_bars "
                "WHERE option_type IN ('CE','PE') AND (strike <= 0 OR strike > 1000000) GROUP BY 1"
            ))).all()
            self.note(f"== implausible strikes (date-contaminated parse?): {strike_bad or 'none'}")

            # --- Tripwires added after the 2026-08-11 data-quality report ---
            # (a) Whole sessions with no spot stamped (report Issue 4).
            spotless = (await c.execute(text(
                """
                SELECT symbol, (ts AT TIME ZONE 'Asia/Kolkata')::date AS d
                FROM oi_archive_bars WHERE option_type IN ('CE','PE')
                GROUP BY 1, 2
                HAVING count(*) FILTER (WHERE underlying IS NOT NULL) = 0
                ORDER BY 1, 2
                """
            ))).all()
            self.note(f"== spot-less sessions (Issue-4 class): {len(spotless)}"
                      + (f" → {[(r[0], str(r[1])) for r in spotless[:20]]}" if spotless else ""))

            # (b) Intra-day OI-collapse signature (report Issue 5): the day's
            # CLOSING OI (last 30 min avg) below 25% of the day's maximum. A
            # true feed collapse persists to the close (Jun-10: 127M → 14M);
            # a new weekly's natural morning ramp starts low but ENDS high —
            # min-vs-max alone false-flagged every first trading day.
            collapse = (await c.execute(text(
                """
                WITH per_min AS (
                    SELECT symbol, (ts AT TIME ZONE 'Asia/Kolkata')::date AS d,
                           date_trunc('minute', ts) AS m, option_type,
                           sum(oi) AS tot
                    FROM oi_archive_bars WHERE option_type IN ('CE','PE')
                    GROUP BY 1, 2, 3, 4
                ),
                daily AS (
                    SELECT symbol, d, option_type, max(tot) AS day_max,
                           avg(tot) FILTER (
                               WHERE m >= (d + time '15:00') AT TIME ZONE 'Asia/Kolkata'
                           ) AS close_avg
                    FROM per_min GROUP BY 1, 2, 3
                )
                SELECT symbol, d, option_type,
                       round(100.0 * close_avg / nullif(day_max, 0)) AS close_pct_of_max
                FROM daily
                WHERE close_avg IS NOT NULL AND close_avg < 0.25 * day_max
                ORDER BY 1, 2 LIMIT 20
                """
            ))).all()
            self.note(f"== intra-day OI-collapse days (Issue-5 class): {len(collapse)}"
                      + (f" → {[(r[0], str(r[1]), r[2], int(r[3])) for r in collapse]}" if collapse else ""))

            # (c) Rows still carried from partial dev-machine recordings.
            xts_days = (await c.execute(text(
                "SELECT count(DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date) "
                "FROM oi_archive_bars WHERE token LIKE 'xts:%'"
            ))).scalar()
            self.note(f"== days still on xts: gap-transfer rows (should be 0 after repair): {xts_days}")

            for tbl, label in (
                ("oi_archive_ticks", "tick archive"),
                ("eod_bars", "EOD bars"),
                ("fii_dii_flows", "FII/DII flows"),
                ("news_items", "news items"),
            ):
                try:
                    cnt = (await c.execute(text(f"SELECT count(*) FROM {tbl}"))).scalar()
                    self.note(f"== {label}: {cnt:,} rows")
                except Exception as e:
                    self.note(f"== {label}: unavailable ({e})")

            prog = (await c.execute(text(
                "SELECT status, count(*) FROM oi_backfill_progress GROUP BY 1"
            ))).all()
            self.note(f"== progress ledger: {dict(prog)}")
        self._write_report("validate")

    # ---------------- report ----------------

    def _write_report(self, name: str) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(IST).strftime("%Y%m%d_%H%M")
        path = OUT_DIR / f"{name}_{stamp}.md"
        path.write_text("\n".join(self.report_lines), encoding="utf-8")
        print(f"[report] {path}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["probe", "pull", "pull-ticks", "pull-eod", "pull-flows", "pull-news", "validate"])
    ap.add_argument("--symbol", default="NIFTY", help="NIFTY | SENSEX (pull)")
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--rps", type=float, default=None, help="override TRUEDATA_RATE_LIMIT_RPS")
    ap.add_argument("--workers", type=int, default=4, help="concurrent in-flight requests (rate-governed)")
    ap.add_argument("--index-symbol", default=None, help="vendor index-bar symbol override")
    ap.add_argument("--dry-run", action="store_true", help="pull: stop after the first expiry")
    ap.add_argument("--include-live-days", action="store_true",
                    help="repair pulls: do NOT skip days present in option_oi_snapshots "
                         "(no-double-count then holds at timestamp level, not day level)")
    ap.add_argument("--days", type=int, default=None,
                    help="lookback days (defaults: ticks 6, eod-bhavcopy 130, flows/news 190)")
    ap.add_argument("--years", type=int, default=10, help="pull-eod: index/futures daily depth")
    args = ap.parse_args()

    bf = Backfill(args)
    try:
        dispatch = {
            "probe": bf.probe, "pull": bf.pull, "pull-ticks": bf.pull_ticks,
            "pull-eod": bf.pull_eod, "pull-flows": bf.pull_flows,
            "pull-news": bf.pull_news, "validate": bf.validate,
        }
        await dispatch[args.command]()
    finally:
        await bf.close()


if __name__ == "__main__":
    asyncio.run(main())
