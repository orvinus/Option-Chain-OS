"""Shared helpers for endpoints that accept ``?symbol=``.

Resolves an optional query param to a SymbolEntry, defaulting to the runtime's
active symbol. Returns FastAPI HTTPException on unknown or (when required)
non-F&O symbols so individual endpoints don't repeat the boilerplate.
"""
from __future__ import annotations

from fastapi import HTTPException

from ..market.symbols import SymbolEntry, get_registry
from ..runtime import get_runtime


def resolve_symbol(symbol: str | None) -> SymbolEntry:
    sym = (symbol or get_runtime().active_symbol or "").upper()
    entry = get_registry().get(sym)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {sym!r}")
    return entry


def resolve_fno_symbol(symbol: str | None) -> SymbolEntry:
    entry = resolve_symbol(symbol)
    if not entry.fno_eligible:
        raise HTTPException(
            status_code=409,
            detail={"error": "not_fno_eligible", "symbol": entry.symbol},
        )
    return entry
