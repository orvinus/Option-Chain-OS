"""GET /api/option-chain — latest CE/PE OI + LTP per strike (per symbol)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.time_utils import IST, TIMEFRAME_TO_DELTA, market_open_today
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from ..runtime import get_runtime
from ..services import get_option_chain_full_engine
from .schemas import (
    OptionChainFullResponseOut,
    OptionChainFullStrikeOut,
    OptionChainResponse,
    OptionChainStrike,
)

router = APIRouter(tags=["option-chain"])


# Floored at the latest data day's session open: strikes frozen in a previous
# session (window drift, weekend artifacts) are not part of the current chain.
_LATEST_FULL_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, volume, underlying, ts
    FROM oi_snapshots_unified
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :floor
    ORDER BY strike, option_type, ts DESC
    """
)

_MAX_TS_SQL = text(
    """
    SELECT MAX(ts) AS max_ts
    FROM oi_snapshots_unified
    WHERE symbol = :symbol AND expiry = :expiry
    """
)


async def latest_session_floor(s, symbol: str, expiry) -> "datetime | None":
    """Session-open (09:15 IST) of the most recent trading day with data."""
    row = (await s.execute(_MAX_TS_SQL, {"symbol": symbol, "expiry": expiry})).mappings().first()
    max_ts = row["max_ts"] if row else None
    if max_ts is None:
        return None
    if getattr(max_ts, "tzinfo", None) is None:
        max_ts = max_ts.replace(tzinfo=timezone.utc)
    return market_open_today(max_ts.astimezone(IST)).astimezone(timezone.utc)


@router.get("/option-chain", response_model=OptionChainResponse)
async def option_chain(
    expiry: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
) -> OptionChainResponse:
    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    async with AsyncSessionLocal() as s:
        floor = await latest_session_floor(s, entry.symbol, e)
        rows = (
            (
                await s.execute(
                    _LATEST_FULL_SQL,
                    {"symbol": entry.symbol, "expiry": e, "floor": floor},
                )
            )
            .mappings()
            .all()
        ) if floor is not None else []

    book: dict[int, dict[str, object]] = {}
    spot: Optional[float] = None
    max_ts = None
    for r in rows:
        slot = book.setdefault(r["strike"], {})
        if r["option_type"] == "CE":
            slot["call_oi"] = int(r["oi"])
            slot["call_ltp"] = float(r["ltp"]) if r.get("ltp") is not None else None
            slot["call_volume"] = int(r.get("volume") or 0)
        else:
            slot["put_oi"] = int(r["oi"])
            slot["put_ltp"] = float(r["ltp"]) if r.get("ltp") is not None else None
            slot["put_volume"] = int(r.get("volume") or 0)
        if spot is None and r.get("underlying") is not None:
            spot = float(r["underlying"])
        if max_ts is None or r["ts"] > max_ts:
            max_ts = r["ts"]

    out: list[OptionChainStrike] = []
    for strike in sorted(book):
        out.append(OptionChainStrike(strike=strike, **book[strike]))  # type: ignore[arg-type]

    return OptionChainResponse(
        expiry=e.isoformat(),
        spot=spot,
        asof=max_ts.astimezone(IST).isoformat() if max_ts else None,
        rows=out,
    )


@router.get("/option-chain-full", response_model=OptionChainFullResponseOut)
async def option_chain_full(
    timeframe: str = Query(
        default="5m",
        description="One of " + ",".join(TIMEFRAME_TO_DELTA.keys()),
    ),
    expiry: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    symbol: str | None = Query(default=None),
) -> OptionChainFullResponseOut:
    if timeframe not in TIMEFRAME_TO_DELTA:
        raise HTTPException(400, f"Unsupported timeframe '{timeframe}'.")
    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    engine = get_option_chain_full_engine()
    rt = get_runtime()
    live_spot = rt.latest_spot if entry.symbol == rt.active_symbol else None
    res = await engine.get(timeframe, e, symbol=entry.symbol, live_spot=live_spot)
    return OptionChainFullResponseOut(
        timeframe=res.timeframe,
        expiry=res.expiry,
        spot=res.spot,
        asof=res.asof,
        computed_at=res.computed_at,
        lot_size=res.lot_size,
        rows=[OptionChainFullStrikeOut(**r.__dict__) for r in res.rows],
        synthetic_future=getattr(res, "synthetic_future", None),
        atm_iv=getattr(res, "atm_iv", None),
        ivp=getattr(res, "ivp", None),
    )
