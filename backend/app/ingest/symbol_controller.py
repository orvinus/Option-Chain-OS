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

from dataclasses import dataclass

from ..auth import get_session_manager
from ..core.logging import get_logger
from ..market.scripmaster import InstrumentToken, resolve_option_universe
from ..market.symbols import SymbolEntry, get_registry
from ..market_data import xts_client
from ..runtime import get_runtime

log = get_logger("symbol_controller")


@dataclass
class SwitchResult:
    symbol: str
    display: str
    fno_eligible: bool
    spot: float | None
    expiries: list[str]


def _spot_segment(entry: SymbolEntry) -> int:
    """XTS cash-market segment for the index/equity spot: BSECM(11) for BSE, else NSECM(1)."""
    return xts_client.SEG_BSECM if (entry.exchange or "").upper() == "BSE" else xts_client.SEG_NSECM


async def _resolve_spot_token(entry: SymbolEntry) -> str | None:
    """Return the XTS spot instrument id for ``entry``, caching it on the registry.

    Uses the entry's exchange to pick the cash-market segment (NSECM for NIFTY,
    BSECM for SENSEX) so the index/spot lookup hits the right exchange.
    """
    if entry.spot_token:
        return entry.spot_token
    sess = get_session_manager()
    if not sess.authenticated:
        log.warning("symbol_controller.no_session_for_spot_lookup", symbol=entry.symbol)
        return None

    spot_seg = _spot_segment(entry)

    # Indices: resolve from the XTS index list by display/symbol name.
    if entry.kind == "index":
        try:
            index_map = await xts_client.get_index_list(sess.token, spot_seg)
        except Exception as e:
            log.warning("symbol_controller.indexlist.error", symbol=entry.symbol, error=str(e))
            index_map = {}
        wanted = {entry.display.upper().strip(), entry.symbol.upper().strip()}
        for name, iid in index_map.items():
            if name in wanted or any(w and w in name for w in wanted):
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

    Raises ``KeyError`` if the symbol is not in the registry.
    """
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
        # Without a spot value we cannot pick the strike window — fall back to
        # whatever runtime had (or a 1.0 placeholder so the resolver still
        # iterates the scripmaster).
        spot_for_window = spot or rt.latest_spot or 1.0
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
