"""Fee model — one editable schedule for paper AND live P&L (§8.2).

Seeded with Lakshmishree Broking's real F&O rate (flat ₹17 per executed
order) plus the statutory NSE options charges. Every component applies to an
options ROUND TRIP as follows (turnover = premium × lot_size × lots):

- brokerage:        flat per executed order — two orders per round trip
- STT:              sell-side premium turnover only
- exchange txn:     both sides' premium turnover
- SEBI turnover:    both sides
- IPFT:             both sides
- GST:              on (brokerage + exchange txn + SEBI), both sides
- stamp duty:       buy-side premium turnover only

All rates live in the config document (GlobalConfig.fees) so the user can
correct any figure from the Integrations tab without a deploy.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config_models import FeeConfig


@dataclass(frozen=True)
class FeeBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    ipft: float
    gst: float
    stamp_duty: float

    @property
    def total(self) -> float:
        return round(
            (self.brokerage + self.stt + self.exchange_txn + self.sebi
             + self.ipft + self.gst + self.stamp_duty) * 100
        ) / 100

    def as_dict(self) -> dict[str, float]:
        return {
            "brokerage": self.brokerage,
            "stt": self.stt,
            "exchange_txn": self.exchange_txn,
            "sebi": self.sebi,
            "ipft": self.ipft,
            "gst": self.gst,
            "stamp_duty": self.stamp_duty,
            "total": self.total,
        }


def round_trip_fees(
    fees: FeeConfig,
    *,
    buy_premium: float,
    sell_premium: float,
    lot_size: int,
    lots: int,
) -> FeeBreakdown:
    """Complete cost of buy → sell for an options position."""
    qty = lot_size * lots
    buy_turnover = buy_premium * qty
    sell_turnover = sell_premium * qty
    both = buy_turnover + sell_turnover

    brokerage = fees.brokerage_per_order * 2
    stt = sell_turnover * fees.stt_sell_premium_pct / 100
    exchange_txn = both * fees.exchange_txn_pct / 100
    sebi = both * fees.sebi_turnover_pct / 100
    ipft = both * fees.ipft_pct / 100
    gst = (brokerage + exchange_txn + sebi) * fees.gst_pct / 100
    stamp = buy_turnover * fees.stamp_duty_buy_pct / 100

    r = lambda x: round(x * 100) / 100  # noqa: E731
    return FeeBreakdown(
        brokerage=r(brokerage),
        stt=r(stt),
        exchange_txn=r(exchange_txn),
        sebi=r(sebi),
        ipft=r(ipft),
        gst=r(gst),
        stamp_duty=r(stamp),
    )
