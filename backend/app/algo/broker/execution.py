"""Execution routing — paper simulator vs the live Lakshmishree adapter.

The orchestrator calls ``execute_entry`` / ``execute_exit`` and never knows
which side fulfilled them. Routing per evaluation:

- ``paper_mode`` ON (the default) → the paper simulator: pessimistic
  slippage fills, no broker contact ever.
- ``paper_mode`` OFF → live routing, PERMITTED only when the Interactive API
  is configured (§15: at least one broker connected before live). Configured
  but failing → the entry is refused (never a silent paper fallback — §9's
  "do not silently fail over to nothing"), and a failed EXIT keeps the
  position under management and retries next minute with a critical alert.

Live instrument identity comes from the platform's own XTS scripmaster (the
Interactive API shares instrument IDs with the market-data product).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import structlog

from ...core.notify import notify
from ..config_models import AlgoConfig
from ..paper import buy_fill, sell_fill
from .xts_interactive import (
    XtsInteractiveError,
    XtsTransportError,
    get_interactive_client,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    fill_price: float
    broker_order_id: str = ""      # empty for paper fills
    note: str = ""


# Sessionless instrument resolution fallback: Zerodha's public instruments
# dump. Its exchange_token column IS the NSE/BSE exchange token, which equals
# the XTS exchangeInstrumentID (identity verified contract-by-contract in the
# 2026-07 validation harness and re-verified 2026-08-17). Needed because the
# primary path below requires an authenticated XTS *market-data* session,
# which does not exist when the platform runs on the TrueData feed — observed
# live 2026-08-17: every live order would have failed to resolve.
_KITE_DUMP = {
    "NIFTY": "https://api.kite.trade/instruments/NFO",
    "SENSEX": "https://api.kite.trade/instruments/BFO",
}
# (symbol, YYYY-MM-DD) -> {(expiry-iso, strike, CE|PE): exchange_token}
_kite_cache: dict = {}


async def _kite_exchange_token(
    symbol: str, expiry, strike: int, option_type: str
) -> Optional[int]:
    from datetime import date as _date

    sym = symbol.upper()
    url = _KITE_DUMP.get(sym)
    if url is None:
        return None
    cache_key = (sym, _date.today().isoformat())
    table = _kite_cache.get(cache_key)
    if table is None:
        import httpx

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            lines = r.text.splitlines()
        if not lines:
            return None
        header = [h.strip().strip('"') for h in lines[0].split(",")]
        idx = {
            k: header.index(k)
            for k in ("exchange_token", "name", "expiry", "strike", "instrument_type")
        }
        table = {}
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < len(header):
                continue
            if parts[idx["name"]].strip('"') != sym:
                continue
            itype = parts[idx["instrument_type"]]
            if itype not in ("CE", "PE"):
                continue
            try:
                table[(parts[idx["expiry"]], int(float(parts[idx["strike"]])), itype)] = int(
                    parts[idx["exchange_token"]]
                )
            except (TypeError, ValueError):
                continue
        _kite_cache.clear()  # keep one day's table per symbol at most
        _kite_cache[cache_key] = table
        log.info("algo.exec.kite_dump_loaded", symbol=sym, contracts=len(table))
    exp_iso = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)[:10]
    return table.get((exp_iso, int(strike), option_type))


def _segment_for(symbol: str) -> str:
    """Contract venue by index: SENSEX/BANKEX options trade on BSE F&O, the
    NSE indices on NSE F&O (same mapping as validation/common.py and
    market/scripmaster.py)."""
    return "BSEFO" if symbol.upper() in ("SENSEX", "BANKEX") else "NSEFO"


async def _resolve_instrument_id(
    symbol: str, expiry, strike: int, option_type: str
) -> Optional[int]:
    """Option contract → XTS exchangeInstrumentID. Primary: the platform's
    XTS scripmaster (needs an authenticated market-data session). Fallback:
    the public Kite dump's exchange_token (identical ID space, no session)."""
    try:
        from ...market.scripmaster import resolve_option_universe

        tokens, _ = await resolve_option_universe(float(strike), symbol)
        for t in tokens:
            if (
                t.strike == strike
                and t.option_type == option_type
                and t.expiry == expiry
            ):
                return int(t.token)
    except Exception as e:
        log.warning("algo.exec.instrument_resolve_failed", error=str(e))
    try:
        token = await _kite_exchange_token(symbol, expiry, strike, option_type)
        if token is not None:
            log.info(
                "algo.exec.instrument_resolved_via_kite",
                symbol=symbol, strike=strike, option_type=option_type, token=token,
            )
            return token
    except Exception as e:
        log.warning("algo.exec.kite_resolve_failed", error=str(e))
    return None


async def execute_entry(
    cfg: AlgoConfig,
    *,
    symbol: str,
    expiry,
    strike: int,
    option_type: str,
    raw_price: float,
    lots: int,
    lot_size: int,
    unique_id: str,
) -> Optional[ExecutionResult]:
    """Buy to open. None = refused (caller skips the entry and alerts)."""
    if cfg.global_.paper.paper_mode:
        fill = buy_fill(cfg.global_.paper, raw_price)
        return ExecutionResult(fill_price=fill.price, note="paper")

    client = get_interactive_client()
    if not client.configured:
        notify(
            "live-broker-unconfigured",
            "🛑 LIVE mode is on but the Lakshmishree Interactive API is not "
            "configured — entry refused. Set the LAKSHMISHREE_INTERACTIVE_* keys "
            "or switch Paper Mode back on.",
        )
        return None
    instrument = await _resolve_instrument_id(symbol, expiry, strike, option_type)
    if instrument is None:
        notify(
            "live-instrument-missing",
            f"🛑 Could not resolve the XTS instrument for {symbol} {strike}"
            f"{option_type} {expiry} — live entry refused.",
        )
        return None
    try:
        order_id = await client.place_market_order(
            exchange_instrument_id=instrument,
            side="BUY",
            quantity=lots * lot_size,
            unique_id=unique_id,
            exchange_segment=_segment_for(symbol),
        )
    except XtsTransportError:
        # The order's fate is UNKNOWN (timeout mid-placement) — it may be
        # live at the exchange. Refusing here would leave an untracked
        # position; bubbling up lets the orchestrator PAUSE and page instead
        # of re-ordering next minute (the repeat-BUY hazard, 2026-08-18).
        raise
    except XtsInteractiveError as e:
        # Definitive gateway refusal — nothing was placed, nothing at risk.
        notify("live-entry-failed", f"🛑 Live entry order failed: {e}")
        return None
    try:
        fill, status = await client.await_fill(order_id, fallback_price=raw_price)
    except XtsTransportError as e:
        # Order PLACED, confirmation unreachable: the position exists. Own it
        # at the decision price and let the reconcile audit true it up.
        notify(
            "live-entry-unconfirmed",
            f"🚨 CRITICAL: entry order {order_id} placed but its fill could "
            f"not be confirmed ({e}) — assuming filled at {raw_price:.2f}. "
            "Verify in the broker app.",
        )
        return ExecutionResult(
            fill_price=raw_price, broker_order_id=order_id, note="fill-unconfirmed"
        )
    except XtsInteractiveError as e:
        # Terminal Rejected/Cancelled — definitively NOT filled.
        notify("live-entry-failed", f"🛑 Live entry order failed: {e}")
        return None
    return ExecutionResult(fill_price=fill, broker_order_id=order_id, note=status)


async def execute_exit(
    cfg: AlgoConfig,
    *,
    symbol: str,
    expiry,
    strike: int,
    option_type: str,
    raw_price: float,
    lots: int,
    lot_size: int,
    unique_id: str,
    entry_order_id: str = "",
) -> Optional[ExecutionResult]:
    """Sell to close. None = the LIVE exit failed — the caller keeps the
    position under management and retries next minute (a paper exit never
    fails)."""
    if not entry_order_id:
        # A position always closes on the ledger it OPENED on, decided solely
        # by whether a broker order backs it. Checking the current paper_mode
        # here was a bug: flipping paper ON mid-live-trade would "close" the
        # ledger row on paper and silently orphan the real broker position.
        fill = sell_fill(cfg.global_.paper, raw_price)
        return ExecutionResult(fill_price=fill.price, note="paper")

    client = get_interactive_client()
    instrument = await _resolve_instrument_id(symbol, expiry, strike, option_type)
    if instrument is None:
        notify(
            "live-exit-instrument",
            f"🚨 CRITICAL: cannot resolve instrument to EXIT {symbol} {strike}"
            f"{option_type} — retrying next minute. Close manually if this repeats!",
        )
        return None
    try:
        order_id = await client.place_market_order(
            exchange_instrument_id=instrument,
            side="SELL",
            quantity=lots * lot_size,
            unique_id=unique_id,
            exchange_segment=_segment_for(symbol),
        )
    except XtsInteractiveError as e:
        # Both definitive refusal AND unknown-state land on retry-next-minute
        # for an EXIT: keeping the position under management (with a page) is
        # strictly safer than assuming it closed. If a lost-in-transit sell
        # DID execute, the broker cleanly rejects the retry and the 5-minute
        # reconcile flags the divergence.
        notify(
            "live-exit-failed",
            f"🚨 CRITICAL: live EXIT order failed ({e}) — position still open, "
            "retrying next minute. Close manually if this repeats!",
        )
        return None
    try:
        fill, status = await client.await_fill(order_id, fallback_price=raw_price)
    except XtsTransportError as e:
        # Sell PLACED but unconfirmed: assume it filled at the decision price
        # (a market sell on a liquid weekly fills) — never retry a second
        # sell on top of a probably-executed one.
        notify(
            "live-exit-unconfirmed",
            f"🚨 CRITICAL: exit order {order_id} placed but its fill could "
            f"not be confirmed ({e}) — assuming closed at {raw_price:.2f}. "
            "Verify in the broker app.",
        )
        return ExecutionResult(
            fill_price=raw_price, broker_order_id=order_id, note="fill-unconfirmed"
        )
    except XtsInteractiveError as e:
        notify(
            "live-exit-failed",
            f"🚨 CRITICAL: live EXIT order failed ({e}) — position still open, "
            "retrying next minute. Close manually if this repeats!",
        )
        return None
    return ExecutionResult(fill_price=fill, broker_order_id=order_id, note=status)


async def reconcile_live_position(
    *,
    symbol: str,
    expiry,
    strike: int,
    option_type: str,
    expected_qty: int,
) -> tuple[Optional[bool], str]:
    """Compare the broker's net quantity for OUR contract with what the
    orchestrator believes it holds. Returns (match, detail):

    - (True, …)  — broker agrees.
    - (False, …) — divergence: squared off manually, a fill we assumed never
      happened, or an unnoticed partial. The caller must alert CRITICALLY.
    - (None, …)  — cannot verify (unconfigured / instrument unresolved /
      gateway error). Not proof of a problem — but say so.

    Only the algo's own contract is inspected: manual positions the user
    holds in other instruments are none of the engine's business.
    """
    client = get_interactive_client()
    if not client.configured:
        return None, "Interactive API not configured"
    instrument = await _resolve_instrument_id(symbol, expiry, strike, option_type)
    if instrument is None:
        return None, f"instrument unresolved for {symbol} {strike}{option_type}"
    try:
        rows = await client.positions_net()
    except XtsInteractiveError as e:
        return None, f"positions fetch failed: {e}"
    # NSE and BSE instrument-id spaces are independent — with SENSEX (BSEFO)
    # now reachable, an id match alone could hit an unrelated NSEFO position.
    # Accept the row only when its segment matches ours (or is absent, for
    # gateway variants that omit it). XTS spells segments as strings and as
    # numeric codes (NSEFO=2, BSEFO=12).
    seg = _segment_for(symbol)
    seg_accept = {seg, {"NSEFO": "2", "BSEFO": "12"}.get(seg, seg)}
    broker_qty = 0
    for r in rows:
        rid = r.get("ExchangeInstrumentId", r.get("ExchangeInstrumentID"))
        if str(rid) != str(instrument):
            continue
        row_seg = str(r.get("ExchangeSegment", "")).strip().upper()
        if row_seg and row_seg not in seg_accept:
            continue
        try:
            broker_qty = int(float(r.get("Quantity") or r.get("NetQty") or 0))
        except (TypeError, ValueError):
            broker_qty = 0
        break
    detail = f"broker holds {broker_qty}, engine expects {expected_qty} (instrument {instrument})"
    return broker_qty == expected_qty, detail


ExecKind = Literal["paper", "live"]


def build_execution() -> tuple[ExecKind, str]:
    """(mode, human status) for the Integrations panel."""
    client = get_interactive_client()
    if client.configured:
        return "live", (
            "configured — awaiting burn-in"
            if not client.logged_in
            else "connected (session live)"
        )
    return "paper", "not configured (LAKSHMISHREE_INTERACTIVE_* unset)"
