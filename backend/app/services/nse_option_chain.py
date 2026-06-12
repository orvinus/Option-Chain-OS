"""Fetch NSE India option chain data for cross-checking our XTS broker feed.

Sources:
  - Indices: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
  - Equities: https://www.nseindia.com/api/option-chain-equities?symbol=SBIN

NSE requires a browser-like session (cookie from the homepage) — same pattern
as ``nifty_public_quote.py``.

NSE expiry dates come as strings like "26-Dec-2024"; we normalise them to
``datetime.date`` objects before returning.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import httpx

from ..core.logging import get_logger

log = get_logger("nse_option_chain")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# NSE symbols that should use the index endpoint instead of equities.
_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYMIDCPSELECT", "MIDCPNIFTY", "NIFTYNXT50"}
# NSE uses these names for the index option chain (may differ from our internal names).
_SYMBOL_TO_NSE = {
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "NIFTYMIDCPSELECT",
    "NIFTYNXT50": "NIFTYNXT50",
}


@dataclass(frozen=True)
class NSEOptionRow:
    strike: int
    expiry: date
    ce_ltp: float | None
    pe_ltp: float | None
    ce_oi: int | None
    pe_oi: int | None


def _parse_nse_expiry(s: str) -> date | None:
    """Parse NSE expiry strings like '26-Dec-2024' or '26-DEC-2024'."""
    s = (s or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _nse_symbol(symbol: str) -> tuple[str, str]:
    """Return (nse_api_symbol, endpoint_type) for the given internal symbol."""
    sym = symbol.upper()
    endpoint = "option-chain-indices" if sym in _INDEX_SYMBOLS else "option-chain-equities"
    nse_sym = _SYMBOL_TO_NSE.get(sym, sym)
    return nse_sym, endpoint


def _parse_response(payload: dict) -> list[NSEOptionRow]:
    records = payload.get("records") or {}
    data = records.get("data") or []
    rows: list[NSEOptionRow] = []
    for item in data:
        strike_raw = item.get("strikePrice")
        expiry_raw = item.get("expiryDate")
        if strike_raw is None or expiry_raw is None:
            continue
        expiry = _parse_nse_expiry(str(expiry_raw))
        if expiry is None:
            continue
        try:
            strike = int(float(strike_raw))
        except (TypeError, ValueError):
            continue
        ce = item.get("CE") or {}
        pe = item.get("PE") or {}

        def _f(d: dict, key: str) -> float | None:
            v = d.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _i(d: dict, key: str) -> int | None:
            v = d.get(key)
            try:
                return int(float(v)) if v is not None else None
            except (TypeError, ValueError):
                return None

        rows.append(NSEOptionRow(
            strike=strike,
            expiry=expiry,
            ce_ltp=_f(ce, "lastPrice"),
            pe_ltp=_f(pe, "lastPrice"),
            ce_oi=_i(ce, "openInterest"),
            pe_oi=_i(pe, "openInterest"),
        ))
    return rows


async def fetch_nse_option_chain(
    symbol: str,
    expiry_filter: date | None = None,
) -> tuple[list[NSEOptionRow], str]:
    """Return (rows, source_endpoint) for the given symbol.

    ``expiry_filter`` restricts rows to a single expiry; None returns all.
    Raises on network/parse failure so callers can catch and degrade gracefully.
    """
    nse_sym, endpoint = _nse_symbol(symbol)
    url = f"https://www.nseindia.com/api/{endpoint}?symbol={nse_sym}"
    option_chain_page = (
        "https://www.nseindia.com/option-chain"
        if endpoint == "option-chain-indices"
        else "https://www.nseindia.com/market-data/equity-stock-indices-details?symbol="
        + nse_sym
    )
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "Cache-Control": "max-age=0",
    }
    api_headers = {
        **headers,
        "Accept": "application/json, text/plain, */*",
        "Referer": option_chain_page,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }
    async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
        # Step 1: Hit the homepage to establish a session cookie.
        for warmup_url in ("https://www.nseindia.com/", option_chain_page):
            try:
                await client.get(warmup_url, headers=headers, timeout=15.0)
                if client.cookies:
                    break  # cookie obtained — stop warming up
            except Exception as e:
                log.warning("nse_option_chain.warmup.error", url=warmup_url, symbol=symbol, error=str(e))

        # Step 2: Fetch the option chain API with the established session.
        resp = await client.get(url, headers=api_headers, timeout=20.0)
        if resp.status_code == 404 and not client.cookies:
            # NSE blocked the warmup (server IP); try the API directly as a last resort.
            log.warning("nse_option_chain.no_cookie_fallback", symbol=symbol)
            resp = await client.get(url, headers=api_headers, timeout=20.0)
        resp.raise_for_status()
        payload = resp.json()

    rows = _parse_response(payload)
    if expiry_filter is not None:
        rows = [r for r in rows if r.expiry == expiry_filter]
    log.info(
        "nse_option_chain.fetched",
        symbol=symbol,
        endpoint=endpoint,
        total=len(rows),
        expiry_filter=expiry_filter.isoformat() if expiry_filter else None,
    )
    return rows, endpoint
