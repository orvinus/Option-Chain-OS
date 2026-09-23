"""GET /api/multi-timeframe — one OI-change row per timeframe + shared level ratio/pcr.

Rows are 1m,3m,5m,10m,15m,30m,1h,2h,3h,full_day by default. Each row's OI-change
is an independent snapshot diff; the header ``ratio``/``pcr``/``spot``/``atm`` are
point-in-time levels shared by every row. ``as_of`` (ISO-8601) computes the grid
as of a historical instant (a picked date or the replay clock); omit for live.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, time

import structlog
from fastapi import APIRouter, HTTPException, Query

from ..core.time_utils import IST, TIMEFRAME_TO_DELTA
from ..services.oi_change import _safe_ratio
from ..runtime import get_runtime
from ..services import get_oi_engine
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import MultiTFResponseOut, MultiTFRowOut

router = APIRouter(tags=["multi-timeframe"])
log = structlog.get_logger(__name__)

DEFAULT_TIMEFRAMES = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "3h", "full_day"]


def _parse_ts(raw: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, f"Invalid '{field}' datetime '{raw}' (expected ISO-8601).")
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt


@router.get("/multi-timeframe", response_model=MultiTFResponseOut)
async def multi_timeframe(
    symbol: str | None = Query(default=None),
    expiry: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    timeframes: str | None = Query(default=None, description="CSV of timeframes; default is the standard 10."),
    as_of: str | None = Query(default=None, description="Historical instant (ISO-8601). Omit for live."),
    atm_window: int | None = Query(default=None, description="Restrict sums to strikes within ATM ± N (omit / <0 = full chain)."),
) -> MultiTFResponseOut:
    tfs = [t.strip() for t in timeframes.split(",")] if timeframes else list(DEFAULT_TIMEFRAMES)
    unknown = [t for t in tfs if t not in TIMEFRAME_TO_DELTA]
    if unknown:
        raise HTTPException(400, f"Unsupported timeframe(s): {', '.join(unknown)}.")

    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    rt = get_runtime()
    as_of_dt = _parse_ts(as_of, "as_of") if as_of is not None else None
    # Live spot only applies to the live (non-historical) active-symbol view.
    live_spot = (
        rt.latest_spot
        if as_of_dt is None and entry.symbol == rt.active_symbol
        else None
    )

    res = await get_oi_engine().get_multi(
        tfs, e, symbol=entry.symbol, live_spot=live_spot, as_of=as_of_dt, atm_window=atm_window
    )
    # The per-timeframe CHANGES come from the engines' own series, so this page
    # and Algo Config can never disagree on the same data (2026-09-23). The
    # snapshot path above still supplies the LEVELS (total OI, PCR, spot).
    rows, atm_strike = [MultiTFRowOut(**r.__dict__) for r in res.rows], res.atm_strike
    engine_rows = await _engine_rows(entry.symbol, e, tfs, as_of_dt, atm_window)
    if engine_rows is not None:
        rows, basket_atm = engine_rows
        if basket_atm is not None:
            atm_strike = basket_atm
    return MultiTFResponseOut(
        symbol=res.symbol,
        expiry=res.expiry,
        asof=res.asof,
        computed_at=res.computed_at,
        spot=res.spot,
        atm_strike=atm_strike,
        total_call_oi=res.total_call_oi,
        total_put_oi=res.total_put_oi,
        ratio=res.ratio,
        pcr=res.pcr,
        rows=rows,
    )


# Timeframes the engine series can produce (mtf_ratio.rows_from_cumulative_series).
_ENGINE_TFS = {"1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "3h", "full_day"}
# Closed-minute data changes once a minute, so one computation serves every
# poll inside that minute (the engine series costs ~3 s; the page polls 15 s).
_ENGINE_CACHE: "OrderedDict[tuple, tuple[list[MultiTFRowOut], int | None]]" = OrderedDict()
_ENGINE_CACHE_MAX = 256


async def _engine_rows(
    symbol: str, expiry, tfs: list[str], as_of_dt: datetime | None, atm_window: int | None
) -> tuple[list[MultiTFRowOut], int | None] | None:
    """Rows from the SAME series Algo Config's Multi-TF panel evaluates, or
    None (fewer than two closed minutes, an unsupported timeframe, or a data
    error) — the caller then keeps the snapshot rows so the page never blanks."""
    if not tfs or any(t not in _ENGINE_TFS for t in tfs):
        return None
    from ..algo.engines import mtf_ratio
    from ..algo.series import mtf_input
    from ..core.time_utils import now_ist
    from .algo_engines import mtf_pair_at

    window = atm_window if (atm_window is not None and atm_window >= 0) else -1
    if as_of_dt is not None:
        ist = as_of_dt.astimezone(IST)
        date_s, cut = ist.date().isoformat(), time(ist.hour, ist.minute)
        minute_key = f"{date_s}T{cut}"
    else:
        # Live: the newest CLOSED minute, exactly like Algo Config's live view.
        date_s, cut = None, None
        minute_key = now_ist().strftime("live-%Y-%m-%dT%H:%M")
    key = (symbol, expiry.isoformat(), window, tuple(tfs), minute_key)
    hit = _ENGINE_CACHE.get(key)
    if hit is not None:
        _ENGINE_CACHE.move_to_end(key)
        return hit
    try:
        pair = await mtf_pair_at(symbol, expiry, window, date_s, cut)
    except Exception as ex:  # noqa: BLE001 — never blank the page over this
        log.warning("multi_timeframe.engine_rows_failed", error=str(ex)[:200])
        return None
    if pair is None:
        return None
    readings = mtf_ratio.rows_from_cumulative_series(*mtf_input(pair), tfs)
    out: list[MultiTFRowOut] = []
    for tf in tfs:
        c_cr, p_cr = readings.get(tf, (0.0, 0.0))
        ce, pe = round(c_cr * 1e7), round(p_cr * 1e7)
        out.append(MultiTFRowOut(
            timeframe=tf, call_oi_change=ce, put_oi_change=pe,
            oi_change_ratio=_safe_ratio(ce, pe),
        ))
    # The basket's centre, so "ATM ± N" names the strikes actually summed.
    basket_atm = (pair.strike_min + pair.strike_max) // 2 if window >= 0 else None
    result = (out, basket_atm)
    _ENGINE_CACHE[key] = result
    while len(_ENGINE_CACHE) > _ENGINE_CACHE_MAX:
        _ENGINE_CACHE.popitem(last=False)
    return result
