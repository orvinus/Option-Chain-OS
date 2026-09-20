"""Option universe resolution from TrueData — the scripmaster's counterpart.

Same machine, different source. The expiry-policy selection, the ATM/window
arithmetic and the strike-step inference are IMPORTED from ``scripmaster`` rather
than reimplemented, because those encode product decisions (which expiries a
policy means, how the window is centred) that must not fork between vendors.

What genuinely differs is discovery:

* XTS ships one pipe-delimited master dump covering every F&O segment, parsed by
  column position.
* TrueData has no such dump for options. Contracts come per (symbol, expiry) from
  ``getSymbolOptionChain``, and the vendor's naming convention appears in NO
  document — so contract strings are ECHOED BACK, never constructed. Strike and
  type are read from the string's tail.

Two measured vendor quirks shape this file:

1. ``getSymbolExpiryList`` returns FUTURE expiries only. On expiry day the
   current expiry can therefore vanish from the list mid-session, and a naive
   "nearest expiry" selector would silently jump the live chain forward a week
   while the market is still trading the near one. The sticky-expiry guard keeps
   serving an expiry until it is genuinely past.

2. A contract whose name does not parse is DROPPED, and the count is logged. It
   is never guessed at, because attributing one contract's open interest to
   another strike is worse than missing it — but a non-zero drop count means an
   unprobed naming convention (MCX and BSE are unverified) and must be visible.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..core.config import settings
from ..core.logging import get_logger
from ..core.time_utils import IST
from ..market_data.truedata_rest import TrueDataRest, parse_expiry_any
from . import td_identity as ident
from .scripmaster import (
    CACHE_TTL,
    InstrumentToken,
    _atm_strike,
    _cache_still_fresh,
    _infer_strike_step,
    _select_expiries,
)
from .symbols import get_registry

log = get_logger("scripmaster_td")

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "scripmaster_td"

# How far ahead to enumerate expiries. Two months covers the current + next
# weekly and the current + next monthly, which is every policy the product has.
EXPIRY_HORIZON_DAYS = 70


# Registry exchange -> the internal F&O tag InstrumentToken.exchange_type maps.
# An option token's exchange must be the DERIVATIVES tag ("NFO"), not the cash
# one ("NSE"): exchange_type does a dict lookup with no default, so "MCX" would
# raise KeyError the first time any consumer touched it.
_FO_TAG = {"NSE": "NFO", "BSE": "BFO", "MCX": "MFO"}


def make_td_token(
    *,
    vendor_symbol: str,
    symbol: str,
    expiry: date,
    strike: int,
    option_type: str,
    exchange: str,
    lotsize: int,
) -> InstrumentToken:
    """Build an ``InstrumentToken`` carrying the vendor's own symbol string.

    ``InstrumentToken`` is a frozen dataclass WITHOUT ``__slots__``, so the extra
    attribute is attached with object.__setattr__ rather than by subclassing.
    That keeps the result a genuine InstrumentToken for every existing consumer
    (runtime.tokens, /api/health's len(), by_exchange, the poller) while the feed
    reads ``vendor_symbol`` via getattr.

    ``vendor_symbol`` is what goes on the wire in addsymbol — echoed back exactly
    as the vendor listed it, never constructed. ``token`` is the archive-
    compatible ``td:`` identity.
    """
    tok = InstrumentToken(
        token=ident.option_token(symbol, expiry, strike, option_type),
        symbol=vendor_symbol,
        name=symbol,
        expiry=expiry,
        strike=int(strike),
        option_type=option_type,
        exchange=_FO_TAG.get(exchange.upper(), "NFO"),
        lotsize=int(lotsize or 0),
    )
    object.__setattr__(tok, "vendor_symbol", vendor_symbol)
    return tok


class _Cache:
    __slots__ = ("contracts", "expiries", "fetched_at")

    def __init__(self, contracts: dict[str, list], expiries: list[date],
                 fetched_at: datetime) -> None:
        self.contracts = contracts   # 'YYMMDD' -> [ (vendor_symbol, strike, type) ]
        self.expiries = expiries
        self.fetched_at = fetched_at


_cache: dict[str, _Cache] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock(symbol: str) -> asyncio.Lock:
    if symbol not in _locks:
        _locks[symbol] = asyncio.Lock()
    return _locks[symbol]


def _disk_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}.json"


def _load_disk(symbol: str) -> _Cache | None:
    p = _disk_path(symbol)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(raw["fetched_at"])
        if fetched.tzinfo is not None:
            # Disk copies written before the UTC-naive fix carry an aware IST
            # stamp — normalise instead of crashing the freshness check.
            fetched = fetched.astimezone(timezone.utc).replace(tzinfo=None)
        if not _cache_still_fresh(fetched):
            return None
        return _Cache(
            contracts={k: [tuple(c) for c in v] for k, v in raw["contracts"].items()},
            expiries=[date.fromisoformat(e) for e in raw["expiries"]],
            fetched_at=fetched,
        )
    except Exception as e:
        log.warning("scripmaster_td.disk_read_failed", symbol=symbol, error=str(e))
        return None


def _save_disk(symbol: str, c: _Cache) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _disk_path(symbol).write_text(
            json.dumps({
                "fetched_at": c.fetched_at.isoformat(),
                "expiries": [e.isoformat() for e in c.expiries],
                "contracts": {k: [list(t) for t in v] for k, v in c.contracts.items()},
            }),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("scripmaster_td.disk_write_failed", symbol=symbol, error=str(e))


async def _discover(symbol: str, td: TrueDataRest, keep: list[date]) -> _Cache:
    """Expiries + per-expiry contract lists for one underlying."""
    today = datetime.now(IST).date()
    raw = await td.get_symbol_expiry_list(symbol)
    parsed = sorted({e for e in (parse_expiry_any(v) for v in raw) if e})
    future = [e for e in parsed if e >= today and e <= today + timedelta(days=EXPIRY_HORIZON_DAYS)]

    # STICKY EXPIRY GUARD. getSymbolExpiryList returns future expiries only, and
    # on expiry day the vendor may already have dropped today's. Re-adding any
    # expiry we are currently serving that has not actually passed prevents the
    # live chain from silently jumping a week forward mid-session.
    sticky = [e for e in keep if e >= today and e not in future]
    if sticky:
        log.info("scripmaster_td.sticky_expiry", symbol=symbol,
                 kept=[str(e) for e in sticky],
                 why="vendor expiry list omitted an expiry we are still serving")
    expiries = sorted(set(future) | set(sticky))
    if not expiries:
        raise RuntimeError(
            f"TrueData returned no usable expiries for {symbol} "
            f"(raw sample: {raw[:5]}) — refusing to cache an empty universe"
        )

    contracts: dict[str, list] = {}
    for exp in expiries:
        key = exp.strftime("%y%m%d")
        rows = await td.get_symbol_option_chain(symbol, key)
        found: list[tuple[str, int, str]] = []
        seen: set[str] = set()
        unparseable = 0
        for r in rows:
            for v in r.values():
                v = (v or "").strip()
                if not v or v in seen:
                    continue
                seen.add(v)
                c = ident.parse_contract(v, symbol, exp)
                if c is None:
                    if v.upper().startswith(symbol[:4].upper()):
                        unparseable += 1
                    continue
                found.append((c.vendor_symbol, c.strike, c.option_type))
        if unparseable:
            # Not fatal, but never silent: an unprobed naming convention (MCX and
            # BSE are unverified) shows up here first, as contracts that exist on
            # the vendor's side but can never be subscribed on ours.
            log.warning("scripmaster_td.unparseable_contracts", symbol=symbol,
                        expiry=str(exp), count=unparseable, parsed=len(found))
        contracts[key] = found

    # _cache_still_fresh's contract is a UTC-NAIVE stamp (it does
    # `datetime.utcnow() - fetched_at`); an aware stamp raises TypeError on
    # every freshness check and killed each failover-poller sweep.
    return _Cache(contracts=contracts, expiries=expiries, fetched_at=datetime.utcnow())


async def get_td_scripmaster(symbol: str, force_refresh: bool = False) -> _Cache:
    """Cached contract universe. Memory -> disk -> vendor, same TTL as XTS."""
    symbol = symbol.upper()
    async with _lock(symbol):
        cur = _cache.get(symbol)
        if not force_refresh and cur is not None and _cache_still_fresh(cur.fetched_at):
            return cur
        if not force_refresh:
            disk = _load_disk(symbol)
            if disk is not None:
                _cache[symbol] = disk
                return disk

        td = TrueDataRest()
        try:
            fresh = await _discover(symbol, td, keep=(cur.expiries if cur else []))
        finally:
            await td.aclose()

        _cache[symbol] = fresh
        _save_disk(symbol, fresh)
        log.info(
            "scripmaster_td.refreshed", symbol=symbol,
            expiries=len(fresh.expiries),
            contracts=sum(len(v) for v in fresh.contracts.values()),
            ttl_hours=CACHE_TTL.total_seconds() / 3600,
        )
        return fresh


async def _configured_data_window() -> int:
    """The effective ATM± strike window: today's ``data_strike_window`` from
    the live Algo Config (user-editable per weekday in Daily Trading Config),
    falling back to the STRIKE_WINDOW env setting on any failure. Read here —
    the single default choke point — so every universe resolution (feed
    subscribe, symbol switch, ATM re-centre) honours the config without the
    callers changing."""
    try:
        from ..algo.config_store import get_config_store

        wd = datetime.now(IST).weekday()
        if wd < 5:
            keys = ("monday", "tuesday", "wednesday", "thursday", "friday")
            day_cfg = (await get_config_store().get_live()).config.days.get(keys[wd])
            if day_cfg is not None and day_cfg.data_strike_window >= 1:
                return int(day_cfg.data_strike_window)
    except Exception:
        pass
    return settings.strike_window


async def resolve_td_option_universe(
    spot: float,
    symbol: str | None = None,
    *,
    window: int | None = None,
    policies: list[str] | None = None,
    force_refresh: bool = False,
    today: date | None = None,
) -> tuple[list[InstrumentToken], list[date]]:
    """Mirror of ``scripmaster.resolve_option_universe`` on the TrueData source.

    Signature-compatible so feed_factory can branch on the vendor and nothing
    downstream notices.
    """
    sym = (symbol or settings.underlying_symbol or "NIFTY").upper()
    cache = await get_td_scripmaster(sym, force_refresh=force_refresh)

    # IST, not UTC: before 05:30 a UTC "today" is still yesterday, which would
    # retain an expiry that has already settled.
    today = today or datetime.now(IST).date()
    win = window if window is not None else await _configured_data_window()
    pols = policies if policies is not None else settings.expiry_policies

    chosen_expiries = _select_expiries(cache.expiries, today, pols)
    if not chosen_expiries:
        return [], []

    entry = get_registry().get(sym)
    exchange = (entry.exchange if entry else "NSE").upper()
    lot = (entry.lot_size if entry else 0) or 0

    all_strikes: list[int] = []
    for exp in chosen_expiries:
        all_strikes.extend(s for (_v, s, _t) in cache.contracts.get(exp.strftime("%y%m%d"), []))
    if not all_strikes:
        return [], chosen_expiries

    step = (entry.strike_step if entry else None) or _infer_strike_step(all_strikes) \
        or settings.strike_step
    atm = _atm_strike(spot, step)
    lo, hi = atm - win * step, atm + win * step

    out: list[InstrumentToken] = []
    for exp in chosen_expiries:
        for vendor_symbol, strike, opt in cache.contracts.get(exp.strftime("%y%m%d"), []):
            if lo <= strike <= hi:
                out.append(make_td_token(
                    vendor_symbol=vendor_symbol, symbol=sym, expiry=exp,
                    strike=strike, option_type=opt, exchange=exchange, lotsize=lot,
                ))
    out.sort(key=lambda t: (t.expiry, t.strike, t.option_type))
    return out, chosen_expiries
