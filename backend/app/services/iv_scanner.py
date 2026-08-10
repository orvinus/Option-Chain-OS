"""Market-wide IV / OI scanner — one row per watchlist symbol.

Aggregates option-chain-full (OI, PCR, ATM IV), Yahoo HV, and iv_daily history
(IVR/IVP/1yr range). Hard-capped at ``settings.iv_scanner_max_symbols``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

from cachetools import TTLCache
from sqlalchemy import text

from ..core.config import settings
from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..core.time_utils import IST
from ..market.symbols import get_registry
from ..market_data.yahoo_hv import compute_hv, fetch_daily_closes, prior_close
from .iv_history import compute_iv_metrics, upsert_atm_iv
from .option_chain_full import get_option_chain_full_engine

log = get_logger("services.iv_scanner")

ExpirySlot = Literal["near", "next", "far"]
ScanMode = Literal["latest", "historical"]

CACHE_TTL_SECONDS = 45


@dataclass
class IvScannerRow:
    symbol: str
    display: str = ""
    price: float | None = None
    price_chg: float | None = None
    price_chg_pct: float | None = None
    total_oi: int = 0
    total_oi_chg: int = 0
    total_oi_chg_pct: float | None = None
    pcr: float | None = None
    iv: float | None = None
    iv_chg_pct: float | None = None
    iv_range_1y_low: float | None = None
    iv_range_1y_high: float | None = None
    hv_10: float | None = None
    hv_20: float | None = None
    hv_30: float | None = None
    ivr: float | None = None
    ivp: float | None = None
    iv_hv10: float | None = None
    iv_hv20: float | None = None
    iv_hv30: float | None = None
    history_ready: bool = False


@dataclass
class IvScannerResponse:
    mode: str
    expiry_slot: str
    expiry: str | None
    asof: str
    max_symbols: int
    rows: list[IvScannerRow] = field(default_factory=list)
    note: str | None = None


_EXPIRIES_SQL = text(
    """
    SELECT DISTINCT expiry
    FROM oi_snapshots_unified
    WHERE symbol = :symbol AND expiry >= CURRENT_DATE
    ORDER BY expiry ASC
    LIMIT 6
    """
)


async def _resolve_expiry_slot(symbol: str, slot: ExpirySlot) -> date | None:
    """Pick near/next/far expiry from DB (falls back to runtime / resolve_expiry)."""
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(_EXPIRIES_SQL, {"symbol": symbol})).scalars().all()
    expiries: list[date] = list(rows)
    if not expiries:
        try:
            from ..api._expiry_utils import resolve_expiry

            return await resolve_expiry(None, symbol=symbol)
        except Exception:
            return None
    idx = {"near": 0, "next": 1, "far": 2}.get(slot, 0)
    if idx >= len(expiries):
        idx = len(expiries) - 1
    return expiries[idx]


def _safe_pct(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return 100.0 * float(num) / float(den)


def _iv_hv_ratio(iv: float | None, hv: float | None) -> float | None:
    if iv is None or hv is None or hv <= 0:
        return None
    return 100.0 * float(iv) / float(hv)


class IvScannerEngine:
    def __init__(self) -> None:
        self._cache: TTLCache = TTLCache(maxsize=256, ttl=CACHE_TTL_SECONDS)
        self._lock = asyncio.Lock()

    async def scan(
        self,
        symbols: list[str],
        *,
        expiry_slot: ExpirySlot = "near",
        mode: ScanMode = "latest",
        timeframe: str = "full_day",
    ) -> IvScannerResponse:
        max_n = settings.iv_scanner_max_symbols
        cleaned: list[str] = []
        seen: set[str] = set()
        for s in symbols:
            u = (s or "").upper().strip()
            if not u or u in seen:
                continue
            seen.add(u)
            cleaned.append(u)
            if len(cleaned) >= max_n:
                break

        note: str | None = None
        if len(symbols) > max_n:
            note = f"Watchlist truncated to {max_n} symbols (IV_SCANNER_MAX_SYMBOLS)."

        cache_key = (tuple(cleaned), expiry_slot, mode, timeframe)
        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            rows = await asyncio.gather(
                *[self._row_for(sym, expiry_slot, mode, timeframe) for sym in cleaned],
                return_exceptions=True,
            )

        out_rows: list[IvScannerRow] = []
        shared_expiry: date | None = None
        for sym, result in zip(cleaned, rows):
            if isinstance(result, Exception):
                log.warning("iv_scanner.row_failed", symbol=sym, error=str(result))
                reg = get_registry().get(sym)
                out_rows.append(
                    IvScannerRow(symbol=sym, display=(reg.display if reg else sym))
                )
                continue
            row, exp = result
            out_rows.append(row)
            if shared_expiry is None and exp is not None:
                shared_expiry = exp

        resp = IvScannerResponse(
            mode=mode,
            expiry_slot=expiry_slot,
            expiry=shared_expiry.isoformat() if shared_expiry else None,
            asof=datetime.now(IST).isoformat(),
            max_symbols=max_n,
            rows=out_rows,
            note=note,
        )
        self._cache[cache_key] = resp
        return resp

    async def _row_for(
        self,
        symbol: str,
        expiry_slot: ExpirySlot,
        mode: ScanMode,
        timeframe: str,
    ) -> tuple[IvScannerRow, date | None]:
        reg = get_registry().get(symbol)
        display = reg.display if reg else symbol
        expiry = await _resolve_expiry_slot(symbol, expiry_slot)

        row = IvScannerRow(symbol=symbol, display=display)
        if expiry is None:
            return row, None

        from ..runtime import get_runtime

        rt = get_runtime()
        live_spot = rt.latest_spot if symbol == rt.active_symbol else None

        engine = get_option_chain_full_engine()
        # historical mode uses full_day baseline; latest uses the requested timeframe
        tf = "full_day" if mode == "historical" else timeframe
        chain = await engine.get(tf, expiry, symbol=symbol, live_spot=live_spot)

        total_call = sum(r.call_oi for r in chain.rows)
        total_put = sum(r.put_oi for r in chain.rows)
        total_call_chg = sum(r.call_oi_change for r in chain.rows)
        total_put_chg = sum(r.put_oi_change for r in chain.rows)
        total_oi = total_call + total_put
        total_oi_chg = total_call_chg + total_put_chg
        prior_oi = total_oi - total_oi_chg

        price = chain.spot
        closes = await fetch_daily_closes(symbol)
        prev = prior_close(closes)
        price_chg = (price - prev) if (price is not None and prev is not None) else None
        price_chg_pct = _safe_pct(price_chg, prev)

        atm_iv = chain.atm_iv
        # Persist today's ATM IV so IVR/IVP accumulate over sessions
        if atm_iv is not None:
            try:
                await upsert_atm_iv(symbol, atm_iv, spot=price, expiry=expiry)
            except Exception as e:
                log.warning("iv_scanner.upsert_failed", symbol=symbol, error=str(e))

        metrics = await compute_iv_metrics(symbol, atm_iv)
        prior_iv = metrics.get("prior_iv")
        iv_chg_pct: float | None = None
        if atm_iv is not None and prior_iv is not None and prior_iv > 0:
            iv_chg_pct = 100.0 * (atm_iv - prior_iv) / prior_iv

        hv = await compute_hv(symbol)

        pcr = (total_put / total_call) if total_call > 0 else None

        row = IvScannerRow(
            symbol=symbol,
            display=display,
            price=price,
            price_chg=price_chg,
            price_chg_pct=price_chg_pct,
            total_oi=total_oi,
            total_oi_chg=total_oi_chg,
            total_oi_chg_pct=_safe_pct(float(total_oi_chg), float(prior_oi)) if prior_oi else None,
            pcr=pcr,
            iv=atm_iv,
            iv_chg_pct=iv_chg_pct,
            iv_range_1y_low=metrics.get("iv_range_1y_low"),
            iv_range_1y_high=metrics.get("iv_range_1y_high"),
            hv_10=hv.get("hv_10"),
            hv_20=hv.get("hv_20"),
            hv_30=hv.get("hv_30"),
            ivr=metrics.get("ivr"),
            ivp=metrics.get("ivp"),
            iv_hv10=_iv_hv_ratio(atm_iv, hv.get("hv_10")),
            iv_hv20=_iv_hv_ratio(atm_iv, hv.get("hv_20")),
            iv_hv30=_iv_hv_ratio(atm_iv, hv.get("hv_30")),
            history_ready=bool(metrics.get("history_ready")),
        )
        return row, expiry


_singleton: IvScannerEngine | None = None


def get_iv_scanner() -> IvScannerEngine:
    global _singleton
    if _singleton is None:
        _singleton = IvScannerEngine()
    return _singleton
