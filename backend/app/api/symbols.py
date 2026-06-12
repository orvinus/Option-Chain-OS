"""Symbol selector endpoints.

GET /api/symbols          — sectored registry tree + the currently active symbol
POST /api/active-symbol   — swap the live WebSocket subscription to a new symbol
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..ingest.symbol_controller import switch_active_symbol
from ..market.symbols import get_registry
from ..runtime import get_runtime
from .schemas import (
    ActiveSymbolRequest,
    ActiveSymbolResponse,
    SymbolEntryOut,
    SymbolSectorGroup,
    SymbolsResponse,
)

router = APIRouter(tags=["symbols"])


@router.get("/symbols", response_model=SymbolsResponse)
async def list_symbols() -> SymbolsResponse:
    reg = get_registry()
    groups = [
        SymbolSectorGroup(
            sector=sector,
            symbols=[
                SymbolEntryOut(
                    symbol=e.symbol,
                    display=e.display,
                    kind=e.kind,
                    sector=e.sector,
                    fno_eligible=e.fno_eligible,
                    lot_size=e.lot_size,
                    strike_step=e.strike_step,
                )
                for e in entries
            ],
        )
        for sector, entries in reg.by_sector()
    ]
    return SymbolsResponse(
        active_symbol=get_runtime().active_symbol,
        groups=groups,
    )


@router.post("/active-symbol", response_model=ActiveSymbolResponse)
async def set_active_symbol(req: ActiveSymbolRequest) -> ActiveSymbolResponse:
    try:
        result = await switch_active_symbol(req.symbol)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return ActiveSymbolResponse(
        symbol=result.symbol,
        display=result.display,
        fno_eligible=result.fno_eligible,
        spot=result.spot,
        expiries=result.expiries,
    )
