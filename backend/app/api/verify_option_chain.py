"""GET /api/verify/option-chain — cross-check XTS option chain data vs NSE India."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..services.nse_option_chain import NSEOptionRow, fetch_nse_option_chain
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import (
    OptionChainCrossCheckResponse,
    OptionChainCrossCheckStrikeOut,
    OptionChainCrossCheckSummary,
)

router = APIRouter(prefix="/verify", tags=["verify"])

log = get_logger("verify_option_chain")

_NSE_FETCH_TIMEOUT_S = 40.0
LTP_TOL_PTS = 5.0
LTP_TOL_PCT = 10.0
OI_TOL_PCT = 10.0

# Floored at the latest data day's session open — the cross-check must compare
# only the strikes that are live in the current session, not rows frozen days
# ago by strike-window drift (those always "fail" vs live NSE data).
_LATEST_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, expiry
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :floor
    ORDER BY strike, option_type, ts DESC
    """
)


def _ltp_aligned(ours: float | None, nse: float | None) -> tuple[float | None, bool | None]:
    if ours is None or nse is None:
        return None, None
    diff = ours - nse
    # Aligned if within ±5 pts OR within ±10% of NSE value (whichever is more lenient).
    tol = max(LTP_TOL_PTS, abs(nse) * LTP_TOL_PCT / 100.0)
    return diff, abs(diff) <= tol


def _oi_aligned(ours: int | None, nse: int | None) -> tuple[float | None, bool | None]:
    if ours is None or nse is None:
        return None, None
    denom = max(nse, 1)
    pct = abs(ours - nse) / denom * 100.0
    return round(pct, 2), pct <= OI_TOL_PCT


@router.get("/option-chain", response_model=OptionChainCrossCheckResponse)
async def option_chain_cross_check(
    symbol: str | None = Query(default=None),
    expiry: str | None = Query(default=None, description="ISO date YYYY-MM-DD; defaults to nearest expiry"),
) -> OptionChainCrossCheckResponse:
    entry = resolve_fno_symbol(symbol)
    expiry_date: date = await resolve_expiry(expiry, symbol=entry.symbol)

    # Fetch our DB data (scoped to the latest session with data).
    from .option_chain import latest_session_floor

    async with AsyncSessionLocal() as sess:
        floor = await latest_session_floor(sess, entry.symbol, expiry_date)
        db_rows = (
            await sess.execute(
                _LATEST_SQL,
                {"symbol": entry.symbol, "expiry": expiry_date, "floor": floor},
            )
        ).mappings().all() if floor is not None else []

    # Organise our data as {strike: {CE: ..., PE: ...}}.
    our_book: dict[int, dict[str, dict]] = {}
    for r in db_rows:
        strike = int(r["strike"])
        otype = str(r["option_type"])
        our_book.setdefault(strike, {})[otype] = {
            "ltp": float(r["ltp"]) if r.get("ltp") is not None else None,
            "oi": int(r["oi"]) if r.get("oi") is not None else None,
        }

    # Fetch NSE data — degrade gracefully when NSE blocks server-side calls.
    nse_rows: list[NSEOptionRow] = []
    nse_source = "unavailable"
    nse_fetch_error: str | None = None
    try:
        nse_rows, nse_source = await asyncio.wait_for(
            fetch_nse_option_chain(entry.symbol, expiry_filter=expiry_date),
            timeout=_NSE_FETCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        nse_fetch_error = "Timed out fetching NSE option chain (>40s). NSE may be slow or blocking."
        log.warning("verify.option_chain.nse_timeout", symbol=entry.symbol)
    except Exception as e:
        nse_fetch_error = f"NSE fetch failed: {e}"
        log.warning("verify.option_chain.nse_error", symbol=entry.symbol, error=str(e))

    # Organise NSE data as {strike: NSEOptionRow}, scaling OI from NSE's unit
    # (CONTRACTS/lots) to ours (units = contracts x market lot) so the two are
    # directly comparable. Without this the OI check is off by ~lot_size (65x
    # for NIFTY) and always fails.
    lot = int(getattr(entry, "lot_size", 0) or 0) or 1
    nse_book: dict[int, NSEOptionRow] = {
        r.strike: NSEOptionRow(
            strike=r.strike,
            expiry=r.expiry,
            ce_ltp=r.ce_ltp,
            pe_ltp=r.pe_ltp,
            ce_oi=r.ce_oi * lot if r.ce_oi is not None else None,
            pe_oi=r.pe_oi * lot if r.pe_oi is not None else None,
        )
        for r in nse_rows
    }

    all_strikes = sorted(our_book.keys() | nse_book.keys())

    out_rows: list[OptionChainCrossCheckStrikeOut] = []
    ce_ltp_ok = pe_ltp_ok = ce_oi_ok = pe_oi_ok = 0
    missing_in_ours = missing_in_nse = 0

    for strike in all_strikes:
        in_ours = strike in our_book
        in_nse = strike in nse_book
        if not in_ours:
            missing_in_ours += 1
        if not in_nse:
            missing_in_nse += 1

        our_ce = our_book.get(strike, {}).get("CE", {})
        our_pe = our_book.get(strike, {}).get("PE", {})
        nse = nse_book.get(strike)

        ce_ltp_diff, ce_ltp_al = _ltp_aligned(our_ce.get("ltp"), nse.ce_ltp if nse else None)
        pe_ltp_diff, pe_ltp_al = _ltp_aligned(our_pe.get("ltp"), nse.pe_ltp if nse else None)
        ce_oi_diff_pct, ce_oi_al = _oi_aligned(our_ce.get("oi"), nse.ce_oi if nse else None)
        pe_oi_diff_pct, pe_oi_al = _oi_aligned(our_pe.get("oi"), nse.pe_oi if nse else None)

        if ce_ltp_al:
            ce_ltp_ok += 1
        if pe_ltp_al:
            pe_ltp_ok += 1
        if ce_oi_al:
            ce_oi_ok += 1
        if pe_oi_al:
            pe_oi_ok += 1

        out_rows.append(OptionChainCrossCheckStrikeOut(
            strike=strike,
            expiry=expiry_date.isoformat(),
            ce_ltp_ours=our_ce.get("ltp"),
            ce_ltp_nse=nse.ce_ltp if nse else None,
            ce_ltp_diff=round(ce_ltp_diff, 2) if ce_ltp_diff is not None else None,
            ce_ltp_aligned=ce_ltp_al,
            ce_oi_ours=our_ce.get("oi"),
            ce_oi_nse=nse.ce_oi if nse else None,
            ce_oi_diff_pct=ce_oi_diff_pct,
            ce_oi_aligned=ce_oi_al,
            pe_ltp_ours=our_pe.get("ltp"),
            pe_ltp_nse=nse.pe_ltp if nse else None,
            pe_ltp_diff=round(pe_ltp_diff, 2) if pe_ltp_diff is not None else None,
            pe_ltp_aligned=pe_ltp_al,
            pe_oi_ours=our_pe.get("oi"),
            pe_oi_nse=nse.pe_oi if nse else None,
            pe_oi_diff_pct=pe_oi_diff_pct,
            pe_oi_aligned=pe_oi_al,
        ))

    log.info(
        "verify.option_chain.done",
        symbol=entry.symbol,
        expiry=expiry_date.isoformat(),
        strikes=len(all_strikes),
        ce_ltp_ok=ce_ltp_ok,
        pe_ltp_ok=pe_ltp_ok,
        ce_oi_ok=ce_oi_ok,
        pe_oi_ok=pe_oi_ok,
    )

    return OptionChainCrossCheckResponse(
        symbol=entry.symbol,
        expiry=expiry_date.isoformat(),
        checked_at=datetime.now(timezone.utc).isoformat(),
        nse_source=nse_source,
        ltp_tolerance_pts=LTP_TOL_PTS,
        ltp_tolerance_pct=LTP_TOL_PCT,
        oi_tolerance_pct=OI_TOL_PCT,
        nse_fetch_error=nse_fetch_error,
        summary=OptionChainCrossCheckSummary(
            total_strikes=len(all_strikes),
            ce_ltp_aligned=ce_ltp_ok,
            pe_ltp_aligned=pe_ltp_ok,
            ce_oi_aligned=ce_oi_ok,
            pe_oi_aligned=pe_oi_ok,
            missing_in_ours=missing_in_ours,
            missing_in_nse=missing_in_nse,
        ),
        rows=out_rows,
    )
