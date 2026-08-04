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

import pytz

from ..core.config import settings
from ..core.logging import get_logger
from ..core.time_utils import IST
from ..market_data import xts_client

log = get_logger("scripmaster")

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "scripmaster"
CACHE_TTL = timedelta(hours=20)

# Legacy cache file (pre-multi-symbol). Read once on first NIFTY load if present.
LEGACY_CACHE_FILE = Path(__file__).resolve().parents[3] / "data" / "scripmaster_cache.json"


def _cache_still_fresh(fetched_at: datetime | None) -> bool:
    """True when a UTC-naive fetch stamp is inside the TTL *and* still on the same
    IST trading date.

    The TTL alone can carry a master across a session boundary — exactly when new
    expiries and strikes get listed — so a stale copy would silently omit them for
    hours. Shared by the per-symbol cache, the module-level FO master cache and the
    disk cache so all three age out together.
    """
    if fetched_at is None:
        return False
    if datetime.utcnow() - fetched_at >= CACHE_TTL:
        return False
    fetched_ist = pytz.utc.localize(fetched_at).astimezone(IST).date()
    return fetched_ist == datetime.now(IST).date()


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
        # XTS ExchangeSegments enum (NOT the legacy Angel codes): NSECM=1, NSEFO=2,
        # NSECD=3, BSECM=11, BSEFO=12, MCXFO=51. SENSEX options live on BSEFO(12);
        # MCX commodity options on MCXFO(51). "MFO" is our internal short tag.
        return {"NSE": 1, "NFO": 2, "CDS": 3, "BSE": 11, "BFO": 12, "MFO": 51}[self.exchange]


@dataclass
class ScripMasterCache:
    raw: list[dict] = field(default_factory=list)
    fetched_at: datetime | None = None

    def fresh(self) -> bool:
        # An empty payload is never "fresh" — see _cache_still_fresh.
        return bool(self.raw) and _cache_still_fresh(self.fetched_at)


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
# Kept in sync with the "Indices" sector in data/symbols.json. Includes BSE indices.
_INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}

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
_COL_MULTIPLIER = 13  # units per lot. MCX carries the real contract size here (LotSize=1)
_COL_UNDERLYING_ID = 14  # UnderlyingInstrumentId — used for MCX near-future ATM resolution
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


# XTS segment prefix (first pipe field) → our internal short exchange tag.
_SEG_PREFIX_TO_TAG = {"NSEFO": "NFO", "BSEFO": "BFO", "MCXFO": "MFO"}


def _master_line_to_row(line: str) -> dict | None:
    """Convert one FO master 'Options' line (NSEFO/BSEFO/MCXFO) into a normalised row.

    Returns ``None`` for non-option / malformed lines. For an MCXFO line that
    fails to parse we emit a debug log — commodity master columns may differ from
    NSEFO, and a silent ``None`` would otherwise produce an empty universe with
    no error (MUST VERIFY the MCX column layout in the Phase-0 probe).
    """
    row = _parse_master_line(line)
    if row is None and line[:5].upper() == "MCXFO":
        log.debug("scripmaster.mcx_line_unparsed", line=line[:160])
    return row


def _parse_master_line(line: str) -> dict | None:
    parts = line.split("|")
    if len(parts) <= _COL_OPTION_TYPE:
        return None
    if parts[_COL_INSTRUMENT_TYPE].strip() != _INSTRUMENT_TYPE_OPTIONS:
        return None
    # parts[0] is the ExchangeSegment ("NSEFO" | "BSEFO" | "MCXFO") — tag the
    # exchange so downstream subscription picks the right XTS segment
    # (NFO=2 / BFO=12 / MCXFO=51). Unknown prefixes default to NFO.
    exch_seg = _SEG_PREFIX_TO_TAG.get(parts[0].strip().upper(), "NFO")
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
    # Commodities (MCX) are neither OPTIDX nor OPTSTK — classify as OPTCOM so the
    # option filter recognises them. Equity/index classification is unchanged.
    if exch_seg == "MFO":
        instrumenttype = "OPTCOM"
    elif series in ("OPTIDX", "OPTSTK", "OPTCOM"):
        instrumenttype = series
    elif name in _INDEX_NAMES:
        instrumenttype = "OPTIDX"
    else:
        instrumenttype = "OPTSTK"
    display = parts[_COL_DISPLAY_NAME].strip() if len(parts) > _COL_DISPLAY_NAME else ""
    underlying_id = parts[_COL_UNDERLYING_ID].strip() if len(parts) > _COL_UNDERLYING_ID else ""
    # MCX carries the tradable contract size in the Multiplier column (col 13);
    # its LotSize (col 12) is 1 (number of lots). NSE/BSE use LotSize directly.
    lot_col = _COL_MULTIPLIER if exch_seg == "MFO" else _COL_LOTSIZE
    lotsize = parts[lot_col].strip() if len(parts) > lot_col else "0"
    return {
        "token": token,
        "symbol": (display or f"{name}{int(strike)}{opt_type}").upper(),
        "name": name,
        "expiry": expiry.strftime("%d%b%Y").upper(),
        "strike": strike,
        "instrumenttype": instrumenttype,
        "exch_seg": exch_seg,
        "lotsize": (lotsize or "0"),
        "option_type": opt_type,
        "underlying_id": underlying_id,
    }


_FO_SEGMENTS = ("NSEFO", "BSEFO", "MCXFO")


async def _load_fo_master_rows(force_refresh: bool = False) -> list[dict]:
    """Fetch + parse the full options master (NSEFO + BSEFO + MCXFO) once, cached for ``CACHE_TTL``.

    All three segments are pulled in one ``/instruments/master`` call so NSE, BSE
    and MCX options share a single cache. BSEFO/MCXFO may be empty/absent if the
    appKey is not entitled to that market — that's tolerated (NSE still works).
    """
    global _master_rows, _master_fetched_at
    async with _master_lock:
        # Truthiness, NOT `is not None`: an EMPTY list used to pass the old guard, so a
        # single bad fetch was served as a valid "no instruments exist" answer for the
        # whole CACHE_TTL. That blanks the option universe and wedges the live feed —
        # no tokens -> no subscribe -> no 'Invalid Token' -> the auth self-heal never
        # fires (production outage 2026-08-03 19:55 IST -> 2026-08-04, whole session).
        if _master_rows and _cache_still_fresh(_master_fetched_at) and not force_refresh:
            return _master_rows

        from ..auth import get_session_manager

        sess = get_session_manager()
        if not sess.authenticated:
            raise RuntimeError("Cannot fetch scrip data: session not authenticated.")

        log.info("scripmaster.master.start")
        dump = await xts_client.get_master(sess.token, list(_FO_SEGMENTS))
        rows: list[dict] = []
        counts = {"NSEFO": 0, "BSEFO": 0, "MCXFO": 0}
        for line in dump.splitlines():
            line = line.strip()
            seg = line[:5].upper()
            if seg not in _FO_SEGMENTS:
                continue
            row = _master_line_to_row(line)
            if row:
                rows.append(row)
                counts[seg] += 1
        if not rows:
            # A real FO master is never empty. Zero parsed rows means an error envelope
            # (XTS returns some failures as HTTP 200 with a string body), a truncated
            # response, or an entitlement change. Leave the previous cache untouched and
            # raise so the ws supervisor retries on its next cycle (<= 60s) instead of
            # us pinning an empty universe for CACHE_TTL.
            log.error(
                "scripmaster.master.empty",
                dump_bytes=len(dump),
                hint="instruments/master returned no NSEFO/BSEFO/MCXFO rows — "
                "usually a dead token or a broker-side error body.",
            )
            raise RuntimeError("XTS instruments/master returned no F&O rows")
        _master_rows = rows
        _master_fetched_at = datetime.utcnow()
        log.info(
            "scripmaster.master.success",
            parsed=len(rows), nsefo=counts["NSEFO"], bsefo=counts["BSEFO"], mcxfo=counts["MCXFO"],
        )
        return rows


async def _fetch_via_master(symbol: str) -> list[dict]:
    """Return the option rows for ``symbol`` from the cached FO master."""
    rows = await _load_fo_master_rows()
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


async def _load_from_disk(symbol: str) -> tuple[list[dict], datetime] | None:
    path = _disk_path(symbol)
    # One-shot migration: if the legacy single-file cache exists and this symbol
    # is NIFTY, fall back to it so we don't lose the warm cache after upgrade.
    if not path.exists() and symbol.upper() == "NIFTY" and LEGACY_CACHE_FILE.exists():
        path = LEGACY_CACHE_FILE
    if not path.exists():
        return None
    try:
        stat = path.stat()
        mtime = datetime.utcfromtimestamp(stat.st_mtime)
        # Same TTL *and* same-IST-trading-date rule as the in-memory caches, so a file
        # written yesterday evening cannot supply today's universe (new expiries and
        # strikes get listed exactly across that boundary).
        if not _cache_still_fresh(mtime):
            return None
        # Return the file's own mtime so the in-memory entry inherits the REAL fetch
        # time. Stamping it with "now" (the old behaviour) restarted the TTL on every
        # load, letting a nearly-expired file live for another full TTL — up to ~40h
        # of staleness, long enough to miss a newly listed expiry or strike.
        return json.loads(path.read_text(encoding="utf-8")), mtime
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
                entry.raw, entry.fetched_at = disk
                return entry.raw
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
    # OPTCOM = MCX commodity option; MFO = MCX F&O segment tag. Both gates must
    # include the commodity variants or every MCX row is silently dropped.
    if (row.get("instrumenttype") or "").upper() not in ("OPTIDX", "OPTSTK", "OPTCOM"):
        return False
    if (row.get("exch_seg") or "").upper() not in ("NFO", "BFO", "MFO"):
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
        # XTS master strikes are in rupees (verified 2026-07-13 across NSE/BSE/MCX):
        # no rescaling. A former ">100000 -> /100" paise-guard was removed — it
        # corrupted legitimate high strikes (SENSEX far-OTM as the index rises,
        # SILVER/GOLD on MCX, high-priced stocks like MRF).
        strike = int(round(float(row.get("strike"))))
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


def _infer_strike_step(strikes: Iterable[int]) -> int:
    """Modal gap between consecutive sorted unique strikes (0 if indeterminate).

    Used when the registry has no per-symbol strike_step (e.g. auto-added stocks
    and MCX commodities, whose steps vary — CRUDEOIL 50, GOLD 100, NATURALGAS 1).
    """
    uniq = sorted({int(s) for s in strikes})
    if len(uniq) < 2:
        return 0
    gaps: dict[int, int] = {}
    for a, b in zip(uniq, uniq[1:]):
        g = b - a
        if g > 0:
            gaps[g] = gaps.get(g, 0) + 1
    if not gaps:
        return 0
    # Most frequent gap; tie-break on the smaller gap (true grid spacing).
    return sorted(gaps.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _atm_from_chain(strike_ltps: dict[int, tuple[float | None, float | None]]) -> int | None:
    """Put–call-parity ATM: the strike minimising ``|call_ltp - put_ltp|``.

    Fallback reference for commodities when the near-future quote is unavailable.
    ``strike_ltps`` maps strike -> (call_ltp, put_ltp); strikes missing either leg
    are ignored.
    """
    best: tuple[float, int] | None = None
    for strike, (call_ltp, put_ltp) in strike_ltps.items():
        if call_ltp is None or put_ltp is None:
            continue
        diff = abs(float(call_ltp) - float(put_ltp))
        if best is None or diff < best[0]:
            best = (diff, int(strike))
    return best[1] if best else None


async def resolve_option_universe(
    spot: float,
    symbol: str | None = None,
    today: date | None = None,
    force_refresh: bool = False,
    window: int | None = None,
    policies: list[str] | None = None,
) -> tuple[list[InstrumentToken], list[date]]:
    """Resolve option tokens for ``symbol`` within the strike window and expiries.

    ``window`` / ``policies`` default to the global ``settings`` values (unchanged
    live-feed behaviour); the universe poller passes a narrower window and its own
    expiry policy to bound REST-quote volume. Returns ``(tokens, chosen_expiries)``.
    """
    sym = (symbol or settings.underlying_symbol).upper()
    raw = await get_scripmaster(sym, force_refresh=force_refresh)
    # IST, not UTC: _select_expiries keeps expiries `>= today`, so between 00:00 and
    # 05:30 IST a UTC date is *yesterday* and an already-expired contract stays in the
    # universe. On expiry day itself the two dates agree, which is why this hid so long.
    today = today or datetime.now(IST).date()
    win = window if window is not None else settings.strike_window
    pols = policies if policies is not None else settings.expiry_policies

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

    expiries = _select_expiries([o.expiry for o in all_options], today, pols)
    if not expiries:
        log.warning("scripmaster.no_expiries_resolved", symbol=sym, policies=pols)
        return [], []

    # Per-symbol strike step from the registry; else infer from the chain's own
    # strike spacing (stocks/commodities), else fall back to settings (50).
    from .symbols import get_registry

    reg_entry = get_registry().get(sym)
    if reg_entry and reg_entry.strike_step > 0:
        step = reg_entry.strike_step
    else:
        step = _infer_strike_step(
            o.strike for o in all_options if o.expiry in expiries
        ) or settings.strike_step

    atm = _atm_strike(spot, step)
    lo = atm - win * step
    hi = atm + win * step

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
