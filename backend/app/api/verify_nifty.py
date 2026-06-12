"""Public cross-check: our NIFTY spot vs NSE India / Yahoo NIFTY 50 quote."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ..runtime import get_runtime
from ..services.nifty_public_quote import best_public_nifty50_reference
from .schemas import NiftyCrossCheckResponse

router = APIRouter(prefix="/verify", tags=["verify"])

_PUBLIC_REF_TIMEOUT_S = 35.0


@router.get("/ping")
async def verify_ping() -> dict[str, str | bool]:
    """No external I/O — use to confirm this router is mounted on the running process."""
    return {
        "ok": True,
        "nifty_cross_check": "/api/verify/nifty-cross-check",
    }


@router.get("/nifty-cross-check", response_model=NiftyCrossCheckResponse)
async def nifty_cross_check() -> NiftyCrossCheckResponse:
    rt = get_runtime()
    our = rt.latest_spot
    try:
        ref, source, detail = await asyncio.wait_for(
            best_public_nifty50_reference(),
            timeout=_PUBLIC_REF_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Timed out fetching public NIFTY 50 reference (NSE/Yahoo). Retry in a moment.",
        ) from None
    diff = float(our - ref) if our is not None and ref is not None else None
    tol = 30.0
    aligned = abs(diff) <= tol if diff is not None else None
    return NiftyCrossCheckResponse(
        our_spot=our,
        reference_last=ref,
        reference_source=source,
        diff_points=diff,
        aligned=aligned,
        tolerance_points=tol,
        fetch_detail=detail,
    )
