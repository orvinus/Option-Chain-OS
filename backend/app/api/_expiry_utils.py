"""Shared helper for parsing/resolving expiry parameters in REST endpoints."""
from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..core.time_utils import now_ist
from ..runtime import get_runtime

log = get_logger("expiry_utils")


async def resolve_expiry(s: str | None, symbol: str | None = None) -> date:
    """Return a ``date`` from the ``expiry`` query param.

    Resolution order when *s* is None:
    1. Runtime list (populated once ingestion starts) — only for the active symbol.
    2. Earliest row in the DB for the given symbol.
    3. 503 if no data exists anywhere.
    """
    if s is not None:
        try:
            return date.fromisoformat(s)
        except ValueError as e:
            raise HTTPException(400, f"Invalid expiry '{s}': {e}") from e

    rt = get_runtime()
    sym = (symbol or rt.active_symbol).upper()
    if sym == rt.active_symbol and rt.expiries:
        return rt.expiries[0]

    # DB fallback — works even when the runtime cache is still empty.
    # Prefer the nearest expiry that has NOT yet settled: a bare MIN(expiry) returns the
    # OLDEST expiry ever stored, so a symbol with history would default to a long-dead
    # contract and still render a full, plausible-looking chain (with fabricated IV).
    # Fall back to the most recent stored expiry only when nothing current exists.
    try:
        async with AsyncSessionLocal() as sess:
            # Unified view (live ∪ vendor archive) so a symbol whose only data is
            # backfilled history (e.g. SENSEX pre-live) still resolves an expiry.
            row = await sess.execute(
                text(
                    "SELECT COALESCE("
                    "  (SELECT MIN(expiry) FROM oi_snapshots_unified"
                    "     WHERE symbol = :sym AND expiry >= :today),"
                    "  (SELECT MAX(expiry) FROM oi_snapshots_unified WHERE symbol = :sym)"
                    ")"
                ),
                {"sym": sym, "today": now_ist().date()},
            )
            val = row.scalar()
        if val:
            return val
    except Exception as e:
        log.warning("expiry_utils.db_fallback.error", error=str(e))

    raise HTTPException(503, "No expiries available yet; ingestion may still be warming up.")
