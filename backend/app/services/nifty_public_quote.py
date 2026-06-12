"""Fetch a public NIFTY 50 last price to sanity-check our broker-fed spot.

Order:
  1. NSE India ``/api/allIndices`` (same index shown on nseindia.com), after a
     homepage hit so cookies/session behave like a browser.
  2. Yahoo Finance ``^NSEI`` chart meta (widely available; tracks the same index).

This is *not* a regulatory certification — only a quick cross-check that our
``latest_spot`` is in the right ballpark during market hours.
"""
from __future__ import annotations

import httpx

from ..core.logging import get_logger

log = get_logger("nifty_public_quote")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


async def _nse_nifty50_from_all_indices(client: httpx.AsyncClient) -> tuple[float | None, str | None]:
    try:
        await client.get("https://www.nseindia.com/", timeout=15.0)
        r = await client.get(
            "https://www.nseindia.com/api/allIndices",
            headers={
                "Referer": "https://www.nseindia.com/",
                "Accept": "application/json",
            },
            timeout=15.0,
        )
        if r.status_code != 200:
            return None, f"nse_allIndices_http_{r.status_code}"
        payload = r.json()
        for row in payload.get("data") or []:
            name = str(row.get("index") or row.get("indexName") or "")
            u = name.upper()
            if "NIFTY 50" in u and "NEXT" not in u:
                raw = row.get("last") or row.get("lastPrice")
                if raw is not None:
                    return float(raw), None
        return None, "nse_nifty50_row_not_found"
    except Exception as e:
        return None, str(e)[:240]


async def _yahoo_nsei_last(client: httpx.AsyncClient) -> tuple[float | None, str | None]:
    try:
        r = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
            "?range=1d&interval=1m",
            timeout=15.0,
        )
        r.raise_for_status()
        chart = r.json().get("chart") or {}
        results = chart.get("result") or []
        if not results:
            return None, "yahoo_empty_result"
        meta = results[0].get("meta") or {}
        if "regularMarketPrice" not in meta:
            return None, "yahoo_no_regular_market_price"
        return float(meta["regularMarketPrice"]), None
    except Exception as e:
        return None, str(e)[:240]


async def best_public_nifty50_reference() -> tuple[float | None, str, str | None]:
    """Return (last_price, source_id, error_or_fallback_detail)."""
    headers = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        nse_val, nse_err = await _nse_nifty50_from_all_indices(client)
        if nse_val is not None:
            return nse_val, "nseindia_allIndices", None
        y_val, y_err = await _yahoo_nsei_last(client)
        if y_val is not None:
            detail = " | ".join(x for x in (nse_err, y_err) if x) or None
            return y_val, "yahoo_nsei", detail
        return None, "none", " | ".join(x for x in (nse_err, y_err) if x) or "all_sources_failed"
