"""Orchestrator: one full validation round.

Layers B and C run first (internal math + frontend ports), then A, D and E
are fetched concurrently so all external sources are snapshotted as close
together as possible. Run twice ~30 min apart for live-market confidence:

    .venv\\Scripts\\python.exe -m validation.run_all --symbol NIFTY --round 1
"""
from __future__ import annotations

import argparse
import asyncio

from . import common, layer_a_broker, layer_b_internal, layer_c_frontend, layer_d_sensibull, layer_e_nse


async def main(symbol: str, round_id: int, skip_internal: bool) -> None:
    print(f"=== validation round {round_id} for {symbol} — market_state={common.market_state()} ===")
    if not skip_internal:
        await layer_b_internal.run(symbol)
        await layer_c_frontend.run(symbol)
    t0 = common.now_utc()
    await asyncio.gather(
        layer_a_broker.run(symbol, round_id),
        layer_d_sensibull.run(symbol, round_id),
        layer_e_nse.run(symbol, round_id),
    )
    dt = (common.now_utc() - t0).total_seconds()
    print(f"=== external snapshot window: {dt:.1f}s ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--skip-internal", action="store_true",
                    help="skip layers B/C (already run this session)")
    args = ap.parse_args()
    asyncio.run(main(args.symbol.upper(), args.round, args.skip_internal))
