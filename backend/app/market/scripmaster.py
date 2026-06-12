"""ScripMaster loader + per-symbol option universe resolver.

Data source priority (per underlying symbol):
  1. In-memory cache (if fresh, < 20h old).
  2. Disk cache (``data/scripmaster/<SYMBOL>.json``, same TTL).
  3. Symphony XTS ``POST /instruments/master`` (NSEFO dump) via the authenticated
     market-data session. The full FO dump is fetched once and filtered per symbol.

Each underlying (NIFTY, BANKNIFTY, RELIANCE, ...) has its own cache entry so
that switching the active symbol does not invalidate prior lookups.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from ..core.config import settings
from ..core.logging import get_logger
from ..market_data import xts_client

log = get_logger("scripmaster")

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "scripmaster"
CACHE_TTL = timedelta(hours=20)

# Legacy cache file (pre-multi-symbol). Read once on first NIFTY load if present.
LEGACY_CACHE_FILE = Path(__file__).resolve().parents[3] / "data" / "scripmaster_cache.json"


@dataclass(frozen=True)
class InstrumentToken:
    token: str
    symbol: str       # e.g. NIFTY08MAY2524800CE
    name: str         # underlying — e.g. 'NIFTY', 'RELIANCE'
    expiry: date
    strike: int
    option_type: str  # 'CE' | 'PE'
    exchange: str     # 'NFO'
    lotsize: int

    @property
    def exchange_type(self) -> int:
        return {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "CDS": 13}[self.exchange]


@dataclass
class ScripMasterCache:
    raw: list[dict] = field(default_factory=list)
    fetched_at: datetime | None = None

    def fresh(self) -> bool:
        return bool(self.raw) and self.fetched_at is not None and (
            datetime.utcnow() - self.fetched_at < CACHE_TTL
        )


# Per-symbol cache and per-symbol lock.
_cache: dict[str, ScripMasterCache] = {}
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(symbol: str) -> asyncio.Lock:
    key = symbol.upper()
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


# ---------------------------------------------------------------- source: XTS master

# Known index underlyings — used to tag instrumenttype correctly when normalising.
# Kept in sync with the "Indices" sector in data/symbols.json.
_INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

# Column indices in an NSEFO "Options" master line (see XTS master Terminology):
# ExchangeSegment|ExchangeInstrumentID|InstrumentType|Name|Description|Series|
# NameWithSeries|InstrumentID|PriceBand.High|PriceBand.Low|FreezeQty|TickSize|
# LotSize|Multiplier|UnderlyingInstrumentId|UnderlyingIndexName|ContractExpiration|
# StrikePrice|OptionType|DisplayName|...
_COL_INSTRUMENT_ID = 1
_COL_INSTRUMENT_TYPE = 2
_COL_NAME = 3
_COL_SERIES = 5
_COL_LOTSIZE = 12
_COL_CONTRACT_EXPIRATION = 16
_COL_STRIKE = 17
_COL_OPTION_TYPE = 18
_COL_DISPLAY_NAME = 19

_INSTRUMENT_TYPE_OPTIONS = "2"  # InstrumentType enum: 2 = Options
# OptionType enum: textual or numeric (3 = CE, 4 = PE).
_OPTION_TYPE_MAP = {"3": "CE", "4": "PE", "CE": "CE", "PE": "PE"}

# Module-level cache of the full parsed NSEFO option rows (shared across symbols
# so switching the active symbol does not re-download the whole FO dump).
_master_rows: list[dict] | None = None
_master_fetched_at: datetime | None = None
_master_lock = asyncio.Lock()


def _parse_iso_expiry(s: str) -> date | None:
    """Parse an XTS ContractExpiration like ``2026-02-17T14:30:00`` → date."""
    s = (s or "").strip()
    if not s:
        return None
    head = s.split("T", 1)[0]
    try:
        return datetime.strptime(head, "%Y-%m-%d").date()
    except ValueError:
        return None


def _master_line_to_row(line: str) -> dict | None:
    """Convert one NSEFO master 'Options' line into a normalised scripmaster row."""
    parts = line.split("|")
    if len(parts) <= _COL_OPTION_TYPE:
        return None
    if parts[_COL_INSTRUMENT_TYPE].strip() != _INSTRUMENT_TYPE_OPTIONS:
        return None
    token = parts[_COL_INSTRUMENT_ID].strip()
    name = parts[_COL_NAME].strip().upper()
    if not token or not name:
        return None
    expiry = _parse_iso_expiry(parts[_COL_CONTRACT_EXPIRATION])
    if expiry is None:
        return None
    opt_type = _OPTION_TYPE_MAP.get(parts[_COL_OPTION_TYPE].strip().upper())
    if not opt_type:
        return None
    try:
        strike = float(parts[_COL_STRIKE].strip())
    except (TypeError, ValueError):
        return None
    series = parts[_COL_SERIES].strip().upper()
    instrumenttype = series if series in ("OPTIDX", "OPTSTK") else (
        "OPTIDX" if name in _INDEX_NAMES else "OPTSTK"
    )
    display = parts[_COL_DISPLAY_NAME].strip() if len(parts) > _COL_DISPLAY_NAME else ""
    return {
        "token": token,
        "symbol": (display or f"{name}{int(strike)}{opt_type}").upper(),
        "name": name,
        "expiry": expiry.strftime("%d%b%Y").upper(),
        "strike": strike,
        "instrumenttype": instrumenttype,
        "exch_seg": "NFO",
        "lotsize": (parts[_COL_LOTSIZE].strip() or "0"),
        "option_type": opt_type,
    }


async def _load_nsefo_master_rows(force_refresh: bool = False) -> list[dict]:
    """Fetch + parse the full NSEFO options master once, cached for ``CACHE_TTL``."""
    global _master_rows, _master_fetched_at
    async with _master_lock:
        fresh = (
            _master_rows is not None
            and _master_fetched_at is not None
            and (datetime.utcnow() - _master_fetched_at < CACHE_TTL)
        )
        if _master_rows is not None and fresh and not force_refresh:
            return _master_rows

        from ..auth import get_session_manager

        sess = get_session_manager()
        if not sess.authenticated:
            raise RuntimeError("Cannot fetch scrip data: session not authenticated.")

        log.info("scripmaster.master.start")
        dump = await xts_client.get_master(sess.token, ["NSEFO"])
        rows: list[dict] = []
        for line in dump.splitlines():
            line = line.strip()
            if not line or not line.upper().startswith("NSEFO"):
                continue
            row = _master_line_to_row(line)
            if row:
                rows.append(row)
        _master_rows = rows
        _master_fetched_at = datetime.utcnow()
        log.info("scripmaster.master.success", parsed=len(rows))
        return rows


async def _fetch_via_master(symbol: str) -> list[dict]:
    """Return the option rows for ``symbol`` from the cached NSEFO master."""
    rows = await _load_nsefo_master_rows()
    sym = symbol.upper()
    out = [r for r in rows if r.get("name") == sym]
    if not out:
        log.warning("scripmaster.master.symbol_empty", symbol=symbol)
    else:
        log.info("scripmaster.master.symbol", symbol=symbol, parsed=len(out))
    return out


# ---------------------------------------------------------------- cache IO


def _disk_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}.json"


async def _load_from_disk(symbol: str) -> list[dict] | None:
    path = _disk_path(symbol)
    # One-shot migration: if the legacy single-file cache exists and this symbol
    # is NIFTY, fall back to it so we don't lose the warm cache after upgrade.
    if not path.exists() and symbol.upper() == "NIFTY" and LEGACY_CACHE_FILE.exists():
        path = LEGACY_CACHE_FILE
    if not path.exists():
        return None
    try:
        stat = path.stat()
        age = datetime.utcnow() - datetime.utcfromtimestamp(stat.st_mtime)
        if age >= CACHE_TTL:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("scripmaster.disk_cache.error", symbol=symbol, error=str(e))
        return None


def _save_to_disk(symbol: str, data: list[dict]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _disk_path(symbol).write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.warning("scripmaster.disk_cache.write_error", symbol=symbol, error=str(e))


async def get_scripmaster(symbol: str | None = None, force_refresh: bool = False) -> list[dict]:
    """Return the (cached) option instrument list for ``symbol`` (default: configured underlying)."""
    sym = (symbol or settings.underlying_symbol).upper()
    async with _get_lock(sym):
        entry = _cache.get(sym)
        if entry is None:
            entry = ScripMasterCache()
            _cache[sym] = entry
        if not force_refresh and entry.fresh():
            return entry.raw
        if not force_refresh:
            disk = await _load_from_disk(sym)
            if disk is not None:
                entry.raw = disk
                entry.fetched_at = datetime.utcnow()
                return disk
        data = await _fetch_via_master(sym)
        entry.raw = data
        entry.fetched_at = datetime.utcnow()
        if data:
            _save_to_disk(sym, data)
        return data


# ---------------------------------------------------------------- parsing


def _parse_expiry(s: str) -> date | None:
    s = (s or "").strip().upper().replace("-", "")
    if not s:
        return None
    for fmt in ("%d%b%Y", "%d%B%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_symbol_option(row: dict, symbol: str) -> bool:
    if (row.get("name") or "").upper() != symbol.upper():
        return False
    if (row.get("instrumenttype") or "").upper() not in ("OPTIDX", "OPTSTK"):
        return False
    if (row.get("exch_seg") or "").upper() != "NFO":
        return False
    return True


def _row_to_token(row: dict) -> InstrumentToken | None:
    expiry = _parse_expiry(row.get("expiry", ""))
    if expiry is None:
        return None
    sym = (row.get("symbol") or "").upper()
    opt_type = (row.get("option_type") or "")
    if not opt_type:
        opt_type = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else ""
    if not opt_type:
        return None
    try:
        raw_strike = row.get("strike")
        strike_val = float(raw_strike)
        if strike_val > 100000:  # paise-scaled (old scrip master format)
            strike_val = strike_val / 100.0
        strike = int(round(strike_val))
    except (TypeError, ValueError):
        return None
    try:
        lot = int(row.get("lotsize") or 0)
    except (TypeError, ValueError):
        lot = 0
    return InstrumentToken(
        token=str(row.get("token")),
        symbol=sym,
        name=(row.get("name") or "").upper(),
        expiry=expiry,
        strike=strike,
        option_type=opt_type,
        exchange=(row.get("exch_seg") or "NFO").upper(),
        lotsize=lot,
    )


# ---------------------------------------------------------------- selection


def _select_expiries(all_expiries: list[date], today: date, policies: list[str]) -> list[date]:
    """Resolve human-friendly policies into concrete expiry dates."""
    future = sorted({e for e in all_expiries if e >= today})
    if not future:
        return []
    weeklies = sorted(future)
    by_month: dict[tuple[int, int], list[date]] = {}
    for e in weeklies:
        by_month.setdefault((e.year, e.month), []).append(e)
    monthlies = sorted({max(v) for v in by_month.values()})

    chosen: list[date] = []
    for p in policies:
        p = p.lower().strip()
        if p == "current_weekly" and weeklies:
            chosen.append(weeklies[0])
        elif p == "next_weekly" and len(weeklies) >= 2:
            chosen.append(weeklies[1])
        elif p == "current_monthly" and monthlies:
            chosen.append(monthlies[0])
        elif p == "next_monthly" and len(monthlies) >= 2:
            chosen.append(monthlies[1])
    seen: set[date] = set()
    result: list[date] = []
    for d in chosen:
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


def _atm_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


async def resolve_option_universe(
    spot: float,
    symbol: str | None = None,
    today: date | None = None,
    force_refresh: bool = False,
) -> tuple[list[InstrumentToken], list[date]]:
    """Resolve option tokens for ``symbol`` within the configured strike window and expiries.

    Returns ``(tokens, chosen_expiries)``.
    """
    sym = (symbol or settings.underlying_symbol).upper()
    raw = await get_scripmaster(sym, force_refresh=force_refresh)
    today = today or datetime.utcnow().date()

    all_options: list[InstrumentToken] = []
    for row in raw:
        if not _is_symbol_option(row, sym):
            continue
        tok = _row_to_token(row)
        if tok:
            all_options.append(tok)

    if not all_options:
        log.warning("scripmaster.no_options_found", symbol=sym)
        return [], []

    expiries = _select_expiries([o.expiry for o in all_options], today, settings.expiry_policies)
    if not expiries:
        log.warning("scripmaster.no_expiries_resolved", symbol=sym, policies=settings.expiry_policies)
        return [], []

    # Per-symbol strike step from the registry; falls back to settings (50).
    from .symbols import get_registry

    reg_entry = get_registry().get(sym)
    step = reg_entry.strike_step if reg_entry and reg_entry.strike_step > 0 else settings.strike_step

    atm = _atm_strike(spot, step)
    lo = atm - settings.strike_window * step
    hi = atm + settings.strike_window * step

    chosen: list[InstrumentToken] = [
        o for o in all_options if o.expiry in expiries and lo <= o.strike <= hi
    ]
    chosen.sort(key=lambda o: (o.expiry, o.strike, o.option_type))

    log.info(
        "scripmaster.universe_resolved",
        symbol=sym,
        spot=spot,
        atm=atm,
        strike_step=step,
        strike_low=lo,
        strike_high=hi,
        expiries=[e.isoformat() for e in expiries],
        token_count=len(chosen),
    )
    return chosen, expiries


def by_exchange(tokens: Iterable[InstrumentToken]) -> dict[int, list[str]]:
    """Group tokens by SmartWebSocketV2 exchange type for ``subscribe()`` payload."""
    out: dict[int, list[str]] = {}
    for t in tokens:
        out.setdefault(t.exchange_type, []).append(t.token)
    return out
