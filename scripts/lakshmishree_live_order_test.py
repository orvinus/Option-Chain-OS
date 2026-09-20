"""M7 burn-in: ONE 1-lot live round-trip on Lakshmishree — IMMEDIATE exit.

RUN THIS YOURSELF in a terminal — it places REAL orders with REAL money:

    cd "<repo root>"
    python scripts\\lakshmishree_live_order_test.py --strike 24000 --type PE

Pick a CHEAP far-OTM current-weekly strike (premium roughly Rs 2-8 — check
it in your broker app first) so the position is as small as possible. One
typed BUY confirmation authorizes the WHOLE round trip: the script buys
1 lot MARKET, fast-polls the fill (every 0.2 s), and the instant the fill
is confirmed it fires the MARKET sell — no second prompt, the position
lives for about a second. Expected cost: the fee model's ~Rs 35-40 for two
orders plus the bid-ask spread — a zero-cost round trip is not possible.

Safety notes:
  * quantity is sent in UNITS (1 lot = 65) — verified against the live
    gateway on 2026-08-14 (the RMS margin figure proved per-unit reading).
  * a BUY rejection leaves NO position and nothing more is sent.
  * the SELL is only fired after the buy fill is CONFIRMED (or the broker's
    position book shows the quantity) — the script can never short you.
  * if anything is left unconfirmed the script says so LOUDLY and stops —
    check the broker app; never assume.
  * credentials come from .env (LAKSHMISHREE_INTERACTIVE_*); nothing is
    printed except masked identity.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

KITE_NFO_DUMP = "https://api.kite.trade/instruments/NFO"


def resolve_contract(strike: int, opt_type: str) -> dict:
    """Current-weekly NIFTY contract row from the public Kite dump."""
    import httpx

    resp = httpx.get(KITE_NFO_DUMP, timeout=30.0)
    resp.raise_for_status()
    rows = [
        r for r in csv.DictReader(io.StringIO(resp.text))
        if r["name"] == "NIFTY" and r["segment"] == "NFO-OPT"
    ]
    expiries = sorted({r["expiry"] for r in rows})
    live = [e for e in expiries if e >= date.today().isoformat()]
    if not live:
        raise SystemExit("no live NIFTY expiry in the instruments dump?!")
    weekly = live[0]
    match = [
        r for r in rows
        if r["expiry"] == weekly
        and float(r["strike"]) == float(strike)
        and r["instrument_type"] == opt_type
    ]
    if not match:
        chain = sorted(
            {int(float(r["strike"])) for r in rows if r["expiry"] == weekly}
        )
        raise SystemExit(
            f"strike {strike}{opt_type} not in the {weekly} chain "
            f"(available {chain[0]}..{chain[-1]} step {chain[1]-chain[0]})"
        )
    return match[0]


def must_type(word: str) -> None:
    got = input(f'>>> type {word} to proceed (anything else aborts): ').strip()
    if got != word:
        raise SystemExit("aborted by user — nothing sent.")


async def fast_poll(client, order_id: str, budget_s: float = 5.0, interval_s: float = 0.2):
    """Poll the order book every 0.2 s until a terminal state or the budget
    runs out — much tighter than the production poller, because here the whole
    point is to exit the position within about a second."""
    deadline = time.monotonic() + budget_s
    while True:
        last = await client.order_result(order_id)
        if last.status in ("Filled", "Rejected", "Cancelled"):
            return last
        if time.monotonic() >= deadline:
            return last
        await asyncio.sleep(interval_s)


async def run(strike: int, opt_type: str, lots: int) -> int:
    from app.algo.broker.xts_interactive import XtsInteractiveClient, XtsInteractiveError
    from app.algo.config_models import default_config
    from app.algo.fees import round_trip_fees

    row = resolve_contract(strike, opt_type)
    token = int(row["exchange_token"])
    lot_size = int(row["lot_size"])
    qty = lots * lot_size

    print("─" * 62)
    print(f" contract   : {row['tradingsymbol']}  (expiry {row['expiry']})")
    print(f" NSE token  : {token}   (exchangeInstrumentID sent to gateway)")
    print(f" quantity   : {lots} lot = {qty} units  MARKET  NRML  NSEFO")
    print(" CHECK THE PREMIUM IN YOUR BROKER APP BEFORE PROCEEDING.")
    print("─" * 62)

    client = XtsInteractiveClient()
    if not client.configured:
        raise SystemExit("LAKSHMISHREE_INTERACTIVE_* not configured in .env")
    await client.login()
    print("login ok (investor client)" if client._is_investor else "login ok (dealer)")

    held_qty = 0
    try:
        must_type("BUY")   # ONE confirmation = buy + IMMEDIATE sell
        try:
            buy_id = await client.place_market_order(
                exchange_instrument_id=token, side="BUY", quantity=qty,
                unique_id="m7burninbuy",
            )
        except XtsInteractiveError as e:
            print(f"\nBUY REJECTED — no position taken. Gateway said:\n  {e}")
            return 1
        t0 = time.monotonic()
        buy = await fast_poll(client, buy_id)
        if buy.status in ("Rejected", "Cancelled"):
            print(f"\nBUY ended {buy.status} — no position, no money moved:\n"
                  f"  {buy.reason or client.last_error or 'no reason given'}")
            return 1
        if buy.status == "Filled":
            held_qty = qty
            print(f"BUY  {buy_id}: Filled @ {buy.average_price}  "
                  f"({time.monotonic() - t0:.2f}s)")
        else:
            # Unconfirmed after the poll budget — trust only the position
            # book before selling (a blind sell could SHORT the account).
            rows = await client.positions_net()
            for r in rows:
                rid = r.get("ExchangeInstrumentId", r.get("ExchangeInstrumentID"))
                if str(rid) == str(token):
                    try:
                        held_qty = abs(int(float(r.get("Quantity") or 0)))
                    except (TypeError, ValueError):
                        held_qty = 0
                    break
            if held_qty < qty:
                print(f"\n!! BUY {buy_id} is still {buy.status or 'unknown'} and the "
                      "position book does not show the quantity — NOT selling "
                      "automatically. Check the broker app and handle the order "
                      "there.")
                return 1
            print(f"BUY  {buy_id}: {buy.status}, but the position book confirms "
                  f"{held_qty} units — proceeding to exit")

        # ── immediate exit, no prompt in between ──
        sell_id = await client.place_market_order(
            exchange_instrument_id=token, side="SELL", quantity=qty,
            unique_id="m7burninsell",
        )
        sell = await fast_poll(client, sell_id)
        if sell.status in ("Rejected", "Cancelled"):
            print(f"\n!! SELL ended {sell.status} — POSITION STILL OPEN, close it "
                  f"manually in the broker app NOW:\n"
                  f"  {sell.reason or client.last_error or 'no reason given'}")
            return 1
        if sell.status != "Filled":
            print(f"\n!! SELL {sell_id} is still {sell.status or 'unknown'} — "
                  "confirm the exit in the broker app before walking away.")
            return 1
        held_qty = 0
        print(f"SELL {sell_id}: Filled @ {sell.average_price}  "
              f"(round trip {time.monotonic() - t0:.2f}s)")

        buy_px = buy.average_price or 0.0
        sell_px = sell.average_price or 0.0
        fees = round_trip_fees(
            default_config().global_.fees,
            buy_premium=buy_px, sell_premium=sell_px,
            lot_size=lot_size, lots=lots,
        )
        gross = (sell_px - buy_px) * qty
        print("─" * 62)
        print(f" gross P&L  : {gross:+.2f}   (≈ the bid-ask spread you crossed)")
        print(f" fees       : {fees.total:.2f}  (brokerage {fees.brokerage:.2f}, "
              f"STT {fees.stt:.2f}, txn {fees.exchange_txn:.2f}, GST {fees.gst:.2f}, "
              f"stamp {fees.stamp_duty:.2f}, SEBI+IPFT {fees.sebi + fees.ipft:.2f})")
        print(f" net P&L    : {gross - fees.total:+.2f}")
        print("─" * 62)
        return 0
    finally:
        if held_qty > 0:
            print("\n!! POSITION MAY STILL BE OPEN — close it manually in your "
                  "broker app NOW.")
        await client.logout()
        print("logged out — session clean.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--strike", type=int, required=True, help="e.g. 24000")
    p.add_argument("--type", choices=("CE", "PE"), required=True)
    p.add_argument("--lots", type=int, default=1)
    a = p.parse_args()
    if a.lots != 1:
        print("burn-in is a ONE-lot test; refusing more.", file=sys.stderr)
        return 2
    return asyncio.run(run(a.strike, a.type, a.lots))


if __name__ == "__main__":
    sys.exit(main())
