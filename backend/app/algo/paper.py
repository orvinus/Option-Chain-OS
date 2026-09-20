"""Paper trading — realistic simulated fills (§8.2).

Fills are computed PESSIMISTICALLY, never optimistically: slippage always
moves the fill against the position (buys fill higher, sells fill lower), so
paper results systematically UNDER-state rather than flatter what live
trading would achieve.

Latency: in the LIVE runtime, ``latency_ms`` is a real wait — the execution
wiring (orchestrator ``_latency_wait``) sleeps it out, capped at 2 s, before
the order reaches the simulator. There is NO re-pricing after the wait: the
fill is the engine's decision price plus the slippage below (re-pricing after
sizing broke the §7 allocation cap and de-anchored the engine's max-SL —
removed 2026-08-18). In the BACKTEST the raw executors are wired directly, so
fills stay deterministic minute-close fills (latency is recorded on the fill
for transparency only).

Fill source: LTP only — the feed does not capture bid/ask. ``PaperConfig``
normalises a stored 'bid_ask_mid' to 'ltp' on load and validate_document
reports the normalisation as a §15 warning.

The fee model is shared with live P&L (fees.py) — both ledgers always speak
the same costs.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config_models import FeeConfig, PaperConfig
from .fees import FeeBreakdown, round_trip_fees


@dataclass(frozen=True)
class PaperFill:
    price: float          # post-slippage fill
    raw_price: float      # the engine's level/decision price
    slippage_pct: float
    latency_ms: int


def buy_fill(paper: PaperConfig, raw_price: float) -> PaperFill:
    """Entry (long option buy): slippage moves the fill UP."""
    price = round(raw_price * (1 + paper.slippage_pct / 100) * 100) / 100
    return PaperFill(
        price=price,
        raw_price=raw_price,
        slippage_pct=paper.slippage_pct,
        latency_ms=paper.latency_ms,
    )


def sell_fill(paper: PaperConfig, raw_price: float) -> PaperFill:
    """Exit (long option sell): slippage moves the fill DOWN."""
    price = round(raw_price * (1 - paper.slippage_pct / 100) * 100) / 100
    return PaperFill(
        price=price,
        raw_price=raw_price,
        slippage_pct=paper.slippage_pct,
        latency_ms=paper.latency_ms,
    )


def round_trip_pnl(
    fees_cfg: FeeConfig,
    *,
    entry_fill: float,
    exit_fill: float,
    lot_size: int,
    lots: int,
) -> tuple[float, FeeBreakdown]:
    """Net rupee P&L after costs + the fee breakdown for the ledger row."""
    gross = (exit_fill - entry_fill) * lot_size * lots
    fb = round_trip_fees(
        fees_cfg,
        buy_premium=entry_fill,
        sell_premium=exit_fill,
        lot_size=lot_size,
        lots=lots,
    )
    net = round((gross - fb.total) * 100) / 100
    return net, fb
