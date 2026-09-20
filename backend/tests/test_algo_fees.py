"""Fee model + paper fill tests — hand-computed against the seeded
Lakshmishree + statutory schedule.

Run:  cd backend && PYTHONPATH=. python tests/test_algo_fees.py
"""
from __future__ import annotations

from app.algo.config_models import FeeConfig, PaperConfig
from app.algo.fees import round_trip_fees
from app.algo.paper import buy_fill, round_trip_pnl, sell_fill


def test_round_trip_fees_hand_computed():
    # Entry 100 / exit 110, lot 75 × 2 lots → qty 150.
    # buy turnover 15,000; sell turnover 16,500; both 31,500.
    fb = round_trip_fees(
        FeeConfig(), buy_premium=100.0, sell_premium=110.0, lot_size=75, lots=2
    )
    assert fb.brokerage == 34.0                     # ₹17 × 2 orders
    assert fb.stt == 16.5                           # 0.1% of sell turnover
    assert abs(fb.exchange_txn - 11.03) < 0.01      # 0.03503% of both sides
    assert abs(fb.sebi - 0.03) < 0.01               # ₹10/crore
    assert abs(fb.ipft - 0.16) < 0.01
    assert abs(fb.gst - 8.11) < 0.01                # 18% on brokerage+txn+SEBI
    assert fb.stamp_duty == 0.45                    # 0.003% of buy side
    assert abs(fb.total - 70.28) < 0.03


def test_round_trip_pnl_nets_fees():
    pnl, fb = round_trip_pnl(
        FeeConfig(), entry_fill=100.0, exit_fill=110.0, lot_size=75, lots=2
    )
    assert abs(pnl - (1500.0 - fb.total)) < 0.01
    # A scratch trade is a NET LOSS by exactly the costs.
    pnl2, fb2 = round_trip_pnl(
        FeeConfig(), entry_fill=100.0, exit_fill=100.0, lot_size=75, lots=1
    )
    assert pnl2 == -fb2.total


def test_fills_are_always_pessimistic():
    paper = PaperConfig(slippage_pct=0.5)
    assert buy_fill(paper, 100.0).price == 100.5, "buys fill HIGHER"
    assert sell_fill(paper, 100.0).price == 99.5, "sells fill LOWER"
    # Zero slippage passes prices through untouched.
    z = PaperConfig(slippage_pct=0.0)
    assert buy_fill(z, 100.0).price == 100.0 and sell_fill(z, 100.0).price == 100.0


def _run_all() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all fee/paper tests passed")


if __name__ == "__main__":
    _run_all()
