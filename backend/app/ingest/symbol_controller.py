"""Active-symbol controller.

Coordinates the switch of the live WebSocket subscription when the dashboard
changes the active symbol via POST /api/active-symbol. SmartAPI caps a single
WS connection at ~1000 tokens, so we keep exactly one underlying live at a
time and swap subscriptions in place rather than running concurrent feeds.

For F&O-eligible symbols this resolves the option universe (strike window
around ATM, configured expiries) and subscribes to those + the spot token.
For non-F&O symbols, only the spot token is subscribed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime

from ..auth import get_session_manager
from ..core.logging import get_logger
from ..core.time_utils import now_ist
from ..market.scripmaster import InstrumentToken, resolve_option_universe
from ..market.symbols import SymbolEntry, get_registry
from ..market_data import xts_client
from ..runtime import get_runtime
from ..services.spot_fallback import db_last_underlying

log = get_logger("symbol_controller")

# Serialises symbol switches (drift-watch re-centre vs. user-initiated switch).
_switch_lock = asyncio.Lock()

# XTS /instruments/indexlist names differ from our F&O symbol codes (e.g. our
# "BANKNIFTY" is "NIFTY BANK" on XTS). Explicit aliases make index spot resolution
# exact instead of relying on fragile substring matching ("NIFTY 50" ⊂ "NIFTY 500").
# Verified against the live indexlist 2026-07-13.
_INDEX_XTS_NAME = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
    "NIFTYNXT50": "NIFTY NEXT 50",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}


@dataclass
class SwitchResult:
    symbol: str
    display: str
    fno_eligible: bool
    spot: float | None
    expiries: list[str]


def _is_commodity(entry: SymbolEntry) -> bool:
    return entry.kind == "commodity" or (entry.exchange or "").upper() == "MCX"


def _spot_segment(entry: SymbolEntry) -> int:
    """XTS segment for the symbol's reference-price instrument.

    Indices/equities use the cash market (NSECM/BSECM). MCX commodities have no
    cash spot — their reference is the near-month FUTURE, which lives on MCXFO(51),
    so the "spot" quote is fetched on the FO segment.
    """
    if _is_commodity(entry):
        return xts_client.SEG_MCXFO
    return xts_client.SEG_BSECM if (entry.exchange or "").upper() == "BSE" else xts_client.SEG_NSECM


async def _resolve_spot_token(entry: SymbolEntry) -> str | None:
    """Return the XTS spot instrument id for ``entry``, caching it on the registry.

    Uses the entry's exchange to pick the cash-market segment (NSECM for NIFTY,
    BSECM for SENSEX) so the index/spot lookup hits the right exchange.
    """
    sess = get_session_manager()

    # Commodities (MCX): no cash spot — the reference is the NEAR-MONTH FUTURE, which
    # ROLLS at monthly expiry. Always re-derive (bypassing any cached spot_token) so
    # the token doesn't go stale after the near contract expires. Cheap: it just
    # filters the in-memory master. Falls back to the last-known token off-session.
    if _is_commodity(entry):
        if not sess.authenticated:
            return entry.spot_token
        return await _resolve_commodity_future_token(entry)

    if entry.spot_token:
        return entry.spot_token
    if not sess.authenticated:
        log.warning("symbol_controller.no_session_for_spot_lookup", symbol=entry.symbol)
        return None

    spot_seg = _spot_segment(entry)

    # Indices: resolve from the XTS index list. Prefer an exact match on the known
    # XTS name (alias), then the symbol/display, then a substring fallback.
    if entry.kind == "index":
        try:
            index_map = await xts_client.get_index_list(sess.token, spot_seg)
        except Exception as e:
            log.warning("symbol_controller.indexlist.error", symbol=entry.symbol, error=str(e))
            index_map = {}
        candidates = {entry.symbol.upper().strip(), (entry.display or "").upper().strip()}
        alias = _INDEX_XTS_NAME.get(entry.symbol.upper())
        if alias:
            candidates.add(alias.upper())
        candidates.discard("")
        # Exact match first (avoids "NIFTY 50" wrongly matching "NIFTY 500").
        for name, iid in index_map.items():
            if name in candidates:
                get_registry().update_spot_token(entry.symbol, iid)
                return iid
        # Substring fallback only if no exact match found.
        for name, iid in index_map.items():
            if any(c in name for c in candidates):
                get_registry().update_spot_token(entry.symbol, iid)
                return iid
        log.warning("symbol_controller.index_token_not_found", symbol=entry.symbol)
        return None

    # Stocks: resolve the NSECM equity instrument id via the search endpoint.
    try:
        items = await xts_client.search_instruments(sess.token, entry.symbol)
    except Exception as e:
        log.warning("symbol_controller.search.error", symbol=entry.symbol, error=str(e))
        return None
    sym = entry.symbol.upper().strip()
    for item in items:
        if int(item.get("ExchangeSegment") or 0) != spot_seg:
            continue
        name = (item.get("Name") or "").upper().strip()
        series = (item.get("Series") or "").upper().strip()
        iid = str(item.get("ExchangeInstrumentID") or "").strip()
        if name == sym and iid and (series in ("", "EQ")):
            get_registry().update_spot_token(entry.symbol, iid)
            return iid
    # Fallback: first NSECM equity match by name.
    for item in items:
        if int(item.get("ExchangeSegment") or 0) != spot_seg:
            continue
        if (item.get("Name") or "").upper().strip() == sym:
            iid = str(item.get("ExchangeInstrumentID") or "").strip()
            if iid:
                get_registry().update_spot_token(entry.symbol, iid)
                return iid
    log.warning("symbol_controller.spot_token_not_found", symbol=entry.symbol)
    return None


async def _resolve_commodity_future_token(entry: SymbolEntry) -> str | None:
    """Near-month future instrument id for an MCX commodity, from the cached master.

    Each option row carries ``underlying_id`` (the future it settles against). We
    pick the earliest non-expired expiry's underlying and cache it as the spot
    token so ATM windowing quotes the near-future price on MCXFO.
    """
    from ..market.scripmaster import _parse_expiry, get_scripmaster

    try:
        rows = await get_scripmaster(entry.symbol)
    except Exception as e:
        log.warning("symbol_controller.commodity_master.error", symbol=entry.symbol, error=str(e))
        return None
    # IST, not UTC — before 05:30 IST a UTC date is yesterday, which would keep an
    # already-expired commodity contract as the "earliest non-expired" spot token.
    today = now_ist().date()
    best: tuple[date, str] | None = None
    for r in rows:
        uid = (r.get("underlying_id") or "").strip()
        exp = _parse_expiry(r.get("expiry", ""))
        if uid and exp and exp >= today and (best is None or exp < best[0]):
            best = (exp, uid)
    if best:
        get_registry().update_spot_token(entry.symbol, best[1])
        return best[1]
    log.warning("symbol_controller.commodity_future_not_found", symbol=entry.symbol)
    return None


async def _fetch_spot_ltp(spot_token: str, spot_seg: int = xts_client.SEG_NSECM) -> float | None:
    sess = get_session_manager()
    if not sess.authenticated:
        return None
    try:
        return await xts_client.quote_ltp(sess.token, spot_seg, spot_token)
    except Exception as e:
        log.warning("symbol_controller.spot_fetch.error", token=spot_token, error=str(e))
        return None


async def switch_active_symbol(symbol: str) -> SwitchResult:
    """Switch the live WS subscription to ``symbol``.

    Serialised: the ATM-drift watcher re-centres by calling this for the CURRENT
    symbol on its own timer, and the dashboard calls it via POST /api/active-symbol.
    Both mutate the same runtime state and both await REST round-trips in the middle,
    so without the lock they interleaved — unsubscribing one symbol's universe while
    subscribing another's, and leaving ``rt.active_symbol`` disagreeing with what the
    feed is actually streaming.

    Raises ``KeyError`` if the symbol is not in the registry.
    """
    async with _switch_lock:
        return await _switch_active_symbol_locked(symbol)


async def _switch_active_symbol_locked(symbol: str) -> SwitchResult:
    reg = get_registry()
    entry = reg.require(symbol)
    sym = entry.symbol
    rt = get_runtime()

    log.info("symbol_controller.switch.start", symbol=sym, fno=entry.fno_eligible)

    spot_seg = _spot_segment(entry)
    spot_token = await _resolve_spot_token(entry)
    spot = await _fetch_spot_ltp(spot_token, spot_seg) if spot_token else None

    feed = rt.feed_client  # OptionFeedClient | None
    tokens: list[InstrumentToken] = []
    expiries_iso: list[str] = []

    if entry.fno_eligible:
        # Without a spot value we cannot pick the strike window. Fall back to
        # the last stored underlying FOR THIS SYMBOL; runtime's latest_spot is
        # only trustworthy when it already belongs to this symbol (using the
        # previous symbol's spot centred a SENSEX window on NIFTY's price ->
        # zero contracts). 1.0 placeholder keeps the resolver iterating.
        db_spot = await db_last_underlying(sym) if spot is None else None
        same_symbol_spot = rt.latest_spot if rt.active_symbol == sym else None
        spot_for_window = spot or db_spot or same_symbol_spot or 1.0
        try:
            tokens, expiries = await resolve_option_universe(
                spot=spot_for_window, symbol=sym
            )
            expiries_iso = [e.isoformat() for e in expiries]
            rt.expiries = expiries
        except Exception as e:
            log.error("symbol_controller.resolve_universe.error", symbol=sym, error=str(e))
            tokens, expiries_iso = [], []
            rt.expiries = []

    if feed is not None and spot_token is not None:
        await feed.swap_subscription(tokens, spot_token, sym, spot_seg)

    rt.tokens = tokens
    if spot is not None:
        rt.latest_spot = spot
    elif entry.fno_eligible and spot_for_window > 1.0:
        # Never leave the previous symbol's spot in runtime — /api/spot and the
        # resubscribe loop would keep serving/centring on the wrong index.
        rt.latest_spot = spot_for_window
    else:
        # No trustworthy spot for the NEW symbol (live quote failed and nothing is
        # stored for it — common for the many symbols that have never been viewed
        # while the poller is off). Without this branch the PREVIOUS symbol's price
        # stayed in runtime and, because active_symbol is reassigned just below,
        # /api/spot would serve it as this symbol's spot. None is the honest value:
        # every consumer already treats a missing spot as "waiting for price".
        rt.latest_spot = None
    rt.active_symbol = sym

    log.info(
        "symbol_controller.switch.done",
        symbol=sym,
        fno=entry.fno_eligible,
        spot=spot,
        token_count=len(tokens),
        expiries=expiries_iso,
    )
    return SwitchResult(
        symbol=sym,
        display=entry.display,
        fno_eligible=entry.fno_eligible,
        spot=spot,
        expiries=expiries_iso,
    )
