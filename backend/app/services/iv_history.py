"""ATM IV daily history — IV Rank, IV Percentile, 1-year IV range, IV change.

Persists one row per (symbol, trade_date) into ``iv_daily``. Metrics that need a
year of history return None (frontend shows "warming up") until enough days
accumulate.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..core.time_utils import IST

log = get_logger("services.iv_history")

# Minimum trading days before IVR/IVP are considered reliable.
MIN_DAYS_FOR_RANK = 20
LOOKBACK_DAYS = 252  # ~1 trading year


_UPSERT_SQL = text(
    """
    INSERT INTO iv_daily (symbol, trade_date, atm_iv, spot, expiry, created_at)
    VALUES (:symbol, :trade_date, :atm_iv, :spot, :expiry, :created_at)
    ON CONFLICT (symbol, trade_date) DO UPDATE SET
        atm_iv = EXCLUDED.atm_iv,
        spot = COALESCE(EXCLUDED.spot, iv_daily.spot),
        expiry = COALESCE(EXCLUDED.expiry, iv_daily.expiry),
        created_at = EXCLUDED.created_at
    """
)

_HISTORY_SQL = text(
    """
    SELECT trade_date, atm_iv
    FROM iv_daily
    WHERE symbol = :symbol
      AND trade_date >= :since
      AND atm_iv IS NOT NULL
    ORDER BY trade_date ASC
    """
)

_TODAY_SQL = text(
    """
    SELECT atm_iv FROM iv_daily
    WHERE symbol = :symbol AND trade_date = :trade_date
    """
)


async def upsert_atm_iv(
    symbol: str,
    atm_iv: float,
    *,
    spot: float | None = None,
    expiry: date | None = None,
    trade_date: date | None = None,
) -> None:
    """Write / refresh today's ATM IV for ``symbol``."""
    if atm_iv is None or atm_iv <= 0:
        return
    d = trade_date or datetime.now(IST).date()
    async with AsyncSessionLocal() as s:
        await s.execute(
            _UPSERT_SQL,
            {
                "symbol": symbol.upper(),
                "trade_date": d,
                "atm_iv": float(atm_iv),
                "spot": spot,
                "expiry": expiry,
                "created_at": datetime.now(timezone.utc),
            },
        )
        await s.commit()


async def fetch_iv_series(symbol: str, lookback_days: int = LOOKBACK_DAYS) -> list[tuple[date, float]]:
    since = date.today() - timedelta(days=int(lookback_days * 1.5))  # calendar buffer
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(_HISTORY_SQL, {"symbol": symbol.upper(), "since": since})
        ).mappings().all()
    return [(r["trade_date"], float(r["atm_iv"])) for r in rows]


def _ivr(current: float, series: Sequence[float]) -> float | None:
    if len(series) < MIN_DAYS_FOR_RANK:
        return None
    lo, hi = min(series), max(series)
    if hi <= lo:
        return 50.0
    return float(100.0 * (current - lo) / (hi - lo))


def _ivp(current: float, series: Sequence[float]) -> float | None:
    if len(series) < MIN_DAYS_FOR_RANK:
        return None
    below = sum(1 for v in series if v <= current)
    return float(100.0 * below / len(series))


async def compute_iv_metrics(symbol: str, current_iv: float | None) -> dict:
    """Return IVR/IVP/1yr-range/prior-day IV for ``symbol``.

    Keys: ivr, ivp, iv_range_1y_low, iv_range_1y_high, prior_iv, history_ready
    """
    empty = {
        "ivr": None,
        "ivp": None,
        "iv_range_1y_low": None,
        "iv_range_1y_high": None,
        "prior_iv": None,
        "history_ready": False,
    }
    if current_iv is None or current_iv <= 0:
        return empty

    series_pairs = await fetch_iv_series(symbol)
    values = [v for _, v in series_pairs]
    history_ready = len(values) >= MIN_DAYS_FOR_RANK

    prior_iv: float | None = None
    today = datetime.now(IST).date()
    for d, v in reversed(series_pairs):
        if d < today:
            prior_iv = v
            break

    return {
        "ivr": _ivr(current_iv, values) if history_ready else None,
        "ivp": _ivp(current_iv, values) if history_ready else None,
        "iv_range_1y_low": min(values) if values else None,
        "iv_range_1y_high": max(values) if values else None,
        "prior_iv": prior_iv,
        "history_ready": history_ready,
    }


async def compute_ivp(symbol: str, current_iv: float) -> float | None:
    metrics = await compute_iv_metrics(symbol, current_iv)
    return metrics.get("ivp")


async def snapshot_atm_iv_for_symbol(symbol: str, timeframe: str = "5m") -> None:
    """Compute today's ATM IV from option-chain-full and persist it."""
    from ..api._expiry_utils import resolve_expiry
    from .option_chain_full import get_option_chain_full_engine

    try:
        expiry = await resolve_expiry(None, symbol=symbol)
        engine = get_option_chain_full_engine()
        res = await engine.get(timeframe, expiry, symbol=symbol)
        if res.atm_iv is None:
            return
        await upsert_atm_iv(
            symbol,
            res.atm_iv,
            spot=res.spot,
            expiry=expiry,
        )
        log.info("iv_history.snapshot", symbol=symbol, atm_iv=res.atm_iv)
    except Exception as e:
        log.warning("iv_history.snapshot_failed", symbol=symbol, error=str(e))
