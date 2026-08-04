"""Thin async REST client for the Shrilakshmi Fintech / Symphony XTS
Binary Market Data API.

This is the single owner of HTTP access to the XTS market-data gateway — the
analog of a broker SDK client. Everything
that needs to talk REST to the broker (session login, instrument master,
quotes, index list, socket subscription) goes through here so the base URL and
auth header live in exactly one place.

All endpoints are relative to ``settings.xts_md_base_url`` (default the Symphony
demo host ``https://developers.symphonyfintech.in/apibinarymarketdata``).

Auth model: ``POST /auth/login`` with ``{secretKey, appKey, source}`` returns a
``token`` (valid ~24h). Every other request carries it in the ``authorization``
header. There is no refresh endpoint for market data — re-login to renew.
"""
from __future__ import annotations

import json
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..core.config import settings
from ..core.logging import get_logger

log = get_logger("xts_client")

DEFAULT_TIMEOUT = 30.0

# Message codes (see XTS "XTS Message Code" enum).
MSG_TOUCHLINE = 1501   # best bid/ask + LTP + volume + OHLC (NO open interest)
MSG_MARKETDEPTH = 1502
MSG_CANDLE = 1505
MSG_OPENINTEREST = 1510  # open interest only

# Exchange segment numeric codes (XTS "ExchangeSegments" enum).
SEG_NSECM = 1
SEG_NSEFO = 2
SEG_NSECD = 3
SEG_BSECM = 11
SEG_BSEFO = 12
# MCX commodity F&O. 51 is the standard Symphony XTS value, but there are ZERO
# MCX references elsewhere in this repo — VERIFY empirically (Phase-0 probe) by
# POST /instruments/master with ["MCXFO"] and inspecting the returned segment tag
# before relying on it. "MFO" is our internal short tag (parallel to NFO/BFO).
SEG_MCXFO = 51

SEGMENT_NAME_TO_CODE = {
    "NSECM": SEG_NSECM, "NSE": SEG_NSECM,
    "NSEFO": SEG_NSEFO, "NFO": SEG_NSEFO,
    "NSECD": SEG_NSECD, "CDS": SEG_NSECD,
    "BSECM": SEG_BSECM, "BSE": SEG_BSECM,
    "BSEFO": SEG_BSEFO, "BFO": SEG_BSEFO,
    "MCXFO": SEG_MCXFO, "MCX": SEG_MCXFO, "MFO": SEG_MCXFO,
}


def base_url() -> str:
    return settings.xts_md_base_url.rstrip("/")


def socket_host_and_path() -> tuple[str, str]:
    """Split the REST base URL into (origin, socketio_path) for python-socketio.

    e.g. ``https://host/apibinarymarketdata`` -> (``https://host``,
    ``/apibinarymarketdata/socket.io``).
    """
    parts = urlsplit(base_url())
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    sio_path = (parts.path.rstrip("/") + "/socket.io") or "/socket.io"
    return origin, sio_path


def authed_headers(token: str) -> dict[str, str]:
    return {"authorization": token, "Content-Type": "application/json"}


def _result(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("result")
    return None


async def login() -> dict[str, str]:
    """``POST /auth/login`` -> ``{"token": ..., "userID": ...}``.

    Uses ``XTS_MD_SECRET_KEY`` / ``XTS_MD_APP_KEY`` / ``XTS_MD_SOURCE`` from env.
    """
    if not (settings.xts_md_app_key or "").strip() or not (settings.xts_md_secret_key or "").strip():
        raise RuntimeError(
            "Missing XTS market-data credentials. Set XTS_MD_APP_KEY and "
            "XTS_MD_SECRET_KEY in your .env file."
        )
    # Market-data login takes only secretKey + appKey (the ``source`` tag is used
    # on the Socket.IO handshake query, not here).
    body = {
        "secretKey": settings.xts_md_secret_key,
        "appKey": settings.xts_md_app_key,
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.post(f"{base_url()}/auth/login", json=body)
        resp.raise_for_status()
        data = resp.json()
    result = _result(data) or {}
    token = result.get("token")
    user_id = result.get("userID") or result.get("userId") or ""
    if not token:
        raise RuntimeError(f"XTS login returned no token: {data!r}")
    return {"token": token, "userID": user_id}


async def logout(token: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            await client.request(
                "DELETE", f"{base_url()}/auth/logout", headers=authed_headers(token)
            )
    except Exception as e:  # best-effort
        log.warning("xts.logout.error", error=str(e))


async def get_master(token: str, segments: Iterable[str]) -> str:
    """``POST /instruments/master`` -> the raw pipe/line-delimited dump string."""
    body = {"exchangeSegmentList": list(segments)}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base_url()}/instruments/master", json=body, headers=authed_headers(token)
        )
        resp.raise_for_status()
        data = resp.json()
    result = _result(data)
    if isinstance(result, str):
        return result
    # Some deployments wrap the dump differently; be tolerant.
    if isinstance(result, dict):
        for key in ("master", "Result", "data"):
            if isinstance(result.get(key), str):
                return result[key]
    raise RuntimeError(f"XTS master returned unexpected shape: {type(result)}")


def _parse_quote_ltp(result: Any) -> float | None:
    """Pull LastTradedPrice out of a /instruments/quotes result."""
    if not isinstance(result, dict):
        return None
    quotes = result.get("listQuotes") or result.get("quoteList") or []
    for q in quotes:
        obj = q
        if isinstance(q, str):
            try:
                obj = json.loads(q)
            except (ValueError, TypeError):
                continue
        if not isinstance(obj, dict):
            continue
        ltp = obj.get("LastTradedPrice")
        if ltp is None and isinstance(obj.get("Touchline"), dict):
            ltp = obj["Touchline"].get("LastTradedPrice")
        if ltp is not None:
            try:
                return float(ltp)
            except (TypeError, ValueError):
                return None
    return None


def parse_quote_entries(payload: Any) -> list[dict]:
    """Normalise a ``/instruments/quotes`` response into a flat list of quote dicts.

    XTS returns each quote either as a nested dict or a JSON-encoded string under
    ``result.listQuotes`` (older gateways use ``quoteList``). Mirrors the parser
    proven in the validation harness so the poller and harness agree byte-for-byte.
    """
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return []
    quotes = result.get("listQuotes") or result.get("quoteList") or []
    out: list[dict] = []
    for q in quotes:
        obj = q
        if isinstance(q, str):
            try:
                obj = json.loads(q)
            except (ValueError, TypeError):
                continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def quote_ltp_volume(q: dict) -> tuple[float | None, int | None]:
    """(LastTradedPrice, TotalTradedQuantity) from a 1501 touchline quote (tolerant of nesting)."""
    tl = q.get("Touchline") if isinstance(q.get("Touchline"), dict) else q
    ltp = tl.get("LastTradedPrice", q.get("LastTradedPrice"))
    vol = tl.get("TotalTradedQuantity", q.get("TotalTradedQuantity"))
    try:
        ltp_f = float(ltp) if ltp is not None else None
    except (TypeError, ValueError):
        ltp_f = None
    try:
        vol_i = int(vol) if vol is not None else None
    except (TypeError, ValueError):
        vol_i = None
    return ltp_f, vol_i


def quote_oi(q: dict) -> int | None:
    """OpenInterest from a 1510 quote (accepts a few key spellings)."""
    for key in ("OpenInterest", "OI", "openInterest"):
        if key in q:
            try:
                return int(q[key])
            except (TypeError, ValueError):
                return None
    return None


async def quote_ltp(token: str, segment: int, instrument_id: int | str) -> float | None:
    """``POST /instruments/quotes`` for one instrument -> its LastTradedPrice (rupees).

    XTS prices are already in rupees, so no scaling.
    """
    body = {
        "instruments": [
            {"exchangeSegment": int(segment), "exchangeInstrumentID": int(instrument_id)}
        ],
        "xtsMessageCode": MSG_TOUCHLINE,
        "publishFormat": "JSON",
    }
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url()}/instruments/quotes", json=body, headers=authed_headers(token)
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("xts.quote.error", segment=segment, instrument=instrument_id, error=str(e))
        return None
    return _parse_quote_ltp(_result(data))


async def get_index_list(token: str, segment: int = SEG_NSECM) -> dict[str, str]:
    """``GET /instruments/indexlist`` -> ``{index_name: instrument_id}``.

    The API returns entries like ``"NIFTY 50_26000"``; we split on the last
    underscore into (name, id).
    """
    out: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url()}/instruments/indexlist",
                params={"exchangeSegment": int(segment)},
                headers=authed_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("xts.indexlist.error", segment=segment, error=str(e))
        return out
    result = _result(data) or {}
    for entry in result.get("indexList", []) or []:
        if not isinstance(entry, str) or "_" not in entry:
            continue
        name, _, iid = entry.rpartition("_")
        if name and iid:
            out[name.strip().upper()] = iid.strip()
    return out


async def search_instruments(token: str, search_string: str) -> list[dict]:
    """``GET /search/instruments?searchString=`` -> list of instrument dicts."""
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url()}/search/instruments",
                params={"searchString": search_string},
                headers=authed_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("xts.search.error", search=search_string, error=str(e))
        return []
    result = _result(data)
    return result if isinstance(result, list) else []


def _subscription_body(instruments: list[dict], xts_message_code: int) -> dict:
    """XTS subscription body: one message code at top level, applied to all instruments.

    Each instrument is ``{"exchangeSegment": int, "exchangeInstrumentID": int}``.
    """
    return {"instruments": instruments, "xtsMessageCode": int(xts_message_code)}


async def subscribe(token: str, instruments: list[dict], xts_message_code: int) -> dict:
    """Subscribe instruments to one message code's stream (``POST /instruments/subscription``).

    NOTE: the broker doc lists PUT for both subscribe and unsubscribe (which
    cannot be distinguished); this follows the standard Symphony XTS SDK
    convention of POST=subscribe / PUT=unsubscribe. If your broker differs,
    flip the HTTP verbs here.
    """
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url()}/instruments/subscription",
            json=_subscription_body(instruments, xts_message_code),
            headers=authed_headers(token),
        )
        resp.raise_for_status()
        return resp.json()


async def unsubscribe(token: str, instruments: list[dict], xts_message_code: int) -> dict:
    """Unsubscribe instruments from one message code's stream (``PUT /instruments/subscription``)."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.put(
            f"{base_url()}/instruments/subscription",
            json=_subscription_body(instruments, xts_message_code),
            headers=authed_headers(token),
        )
        resp.raise_for_status()
        return resp.json()
