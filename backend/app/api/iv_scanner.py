"""GET /api/iv-scanner — multi-symbol IV / OI market scanner."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..core.config import settings
from ..services.iv_scanner import get_iv_scanner
from .schemas import IvScannerResponseOut, IvScannerRowOut

router = APIRouter(tags=["iv-scanner"])


@router.get("/iv-scanner", response_model=IvScannerResponseOut)
async def iv_scanner(
    symbols: str = Query(
        ...,
        description="Comma-separated F&O symbols (max IV_SCANNER_MAX_SYMBOLS).",
    ),
    expiry: str = Query(
        default="near",
        description="Expiry slot: near | next | far",
    ),
    mode: str = Query(
        default="latest",
        description="latest | historical",
    ),
    timeframe: str = Query(
        default="full_day",
        description="OI-change window used for Total OI Chg (e.g. full_day, 1h).",
    ),
) -> IvScannerResponseOut:
    slot = (expiry or "near").lower().strip()
    if slot not in ("near", "next", "far"):
        raise HTTPException(400, "expiry must be one of: near, next, far")
    scan_mode = (mode or "latest").lower().strip()
    if scan_mode not in ("latest", "historical"):
        raise HTTPException(400, "mode must be one of: latest, historical")

    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        raise HTTPException(400, "symbols query param is required (comma-separated).")
    if len(sym_list) > settings.iv_scanner_max_symbols:
        # Truncation is also enforced in the engine; warn via 200+note rather than 400
        # so the UI can still render a partial watchlist.
        pass

    engine = get_iv_scanner()
    res = await engine.scan(
        sym_list,
        expiry_slot=slot,  # type: ignore[arg-type]
        mode=scan_mode,  # type: ignore[arg-type]
        timeframe=timeframe,
    )
    return IvScannerResponseOut(
        mode=res.mode,
        expiry_slot=res.expiry_slot,
        expiry=res.expiry,
        asof=res.asof,
        max_symbols=res.max_symbols,
        rows=[IvScannerRowOut(**r.__dict__) for r in res.rows],
        note=res.note,
    )
