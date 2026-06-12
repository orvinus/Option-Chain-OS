"""GET /api/option-chain — latest CE/PE OI + LTP per strike (per symbol)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.time_utils import IST, TIMEFRAME_TO_DELTA
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


_LATEST_FULL_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, volume, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry
    ORDER BY strike, option_type, ts DESC
    """
)


@router.get("/option-chain", response_model=OptionChainResponse)
async def option_chain(
    expiry: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
) -> OptionChainResponse:
    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    async with AsyncSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    _LATEST_FULL_SQL,
                    {"symbol": entry.symbol, "expiry": e},
                )
            )
            .mappings()
            .all()
        )

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
    )
