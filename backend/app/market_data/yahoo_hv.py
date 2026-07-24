"""Yahoo Finance daily closes → Historical Volatility (HV 10/20/30).

Fetches adjusted closes via Yahoo's public chart API (no API key). Results are
cached in-process for the calendar day so repeated scanner polls are cheap.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from cachetools import TTLCache

from ..core.config import settings
from ..core.logging import get_logger

log = get_logger("market_data.yahoo_hv")

# Cache closes for ~6 hours (same trading day re-uses the series).
_closes_cache: TTLCache = TTLCache(maxsize=512, ttl=6 * 3600)
_hv_cache: TTLCache = TTLCache(maxsize=512, ttl=6 * 3600)

# NSE indices → Yahoo tickers. Equity symbols map to SYMBOL.NS.
_INDEX_YAHOO: dict[str, str] = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
    "SENSEX": "^BSESN",
    "BANKEX": "BSE-BANK.BO",
}


def yahoo_ticker_for(symbol: str) -> str | None:
    """Map an internal F&O symbol to a Yahoo Finance ticker."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    if sym in _INDEX_YAHOO:
        return _INDEX_YAHOO[sym]
    # Commodities / MCX — Yahoo coverage is sparse; skip for now.
    if sym in {"CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER", "ZINC", "LEAD", "NICKEL", "ALUMINIUM", "MENTHAOIL", "COTTON"}:
        return None
    return f"{sym}.NS"


async def fetch_daily_closes(symbol: str, lookback_days: int = 400) -> list[tuple[date, float]]:
    """Return sorted (date, close) pairs for ``symbol``, newest last."""
    if not settings.yahoo_hv_enabled:
        return []
    ticker = yahoo_ticker_for(symbol)
    if not ticker:
        return []

    cache_key = (symbol.upper(), lookback_days, date.today().isoformat())
    cached = _closes_cache.get(cache_key)
    if cached is not None:
        return cached

    now = int(datetime.now(timezone.utc).timestamp())
    period1 = now - lookback_days * 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {
        "period1": period1,
        "period2": now,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; OptionChainOS/1.0)"}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
    except Exception as e:
        log.warning("yahoo_hv.fetch_failed", symbol=symbol, ticker=ticker, error=str(e))
        return []

    try:
        result = payload["chart"]["result"][0]
        timestamps: list[int] = result["timestamp"]
        closes_raw = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as e:
        log.warning("yahoo_hv.parse_failed", symbol=symbol, error=str(e))
        return []

    series: list[tuple[date, float]] = []
    for ts, close in zip(timestamps, closes_raw):
        if close is None:
            continue
        try:
            c = float(close)
        except (TypeError, ValueError):
            continue
        if c <= 0 or not math.isfinite(c):
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        series.append((d, c))

    series.sort(key=lambda x: x[0])
    _closes_cache[cache_key] = series
    return series


def _hv_from_closes(closes: list[float], window: int) -> float | None:
    """Annualized stdev of log returns over the last ``window`` closes."""
    if len(closes) < window + 1:
        return None
    window_closes = closes[-(window + 1) :]
    log_rets: list[float] = []
    for i in range(1, len(window_closes)):
        a, b = window_closes[i - 1], window_closes[i]
        if a <= 0 or b <= 0:
            continue
        log_rets.append(math.log(b / a))
    if len(log_rets) < max(window // 2, 5):
        return None
    mean = sum(log_rets) / len(log_rets)
    var = sum((x - mean) ** 2 for x in log_rets) / max(len(log_rets) - 1, 1)
    daily_std = math.sqrt(var)
    return float(daily_std * math.sqrt(252.0))  # annualized decimal (0.18 = 18%)


async def compute_hv(symbol: str) -> dict[str, float | None]:
    """Return ``{hv_10, hv_20, hv_30}`` as annualized decimals (or None)."""
    cache_key = (symbol.upper(), date.today().isoformat())
    cached = _hv_cache.get(cache_key)
    if cached is not None:
        return cached

    series = await fetch_daily_closes(symbol)
    closes = [c for _, c in series]
    out = {
        "hv_10": _hv_from_closes(closes, 10),
        "hv_20": _hv_from_closes(closes, 20),
        "hv_30": _hv_from_closes(closes, 30),
    }
    _hv_cache[cache_key] = out
    return out


def prior_close(symbol_closes: list[tuple[date, float]], asof: date | None = None) -> float | None:
    """Most recent close on or before ``asof`` (default: yesterday)."""
    if not symbol_closes:
        return None
    cutoff = asof or (date.today() - timedelta(days=1))
    prior = [c for d, c in symbol_closes if d <= cutoff]
    return prior[-1] if prior else None
