"""Smoke test: token reuse + a tiny XTS quotes call + universe fetch. Read-only."""
from __future__ import annotations

import asyncio
import json

import httpx

from . import common


async def main() -> None:
    token, uid, issued = common.load_xts_token()
    print(f"token loaded: user={uid} issued_at={issued.isoformat()} len={len(token)}")

    uni = common.todays_universe("NIFTY")
    print(f"universe rows today: {len(uni)}")
    if len(uni) < 2:
        print("Not enough universe rows yet — wait for flushes.")
        return
    sample = uni[:2]
    print("sample:", [(u.token, u.strike, u.option_type) for u in sample])

    seg = common.option_segment("NIFTY")
    instruments = [
        {"exchangeSegment": seg, "exchangeInstrumentID": int(u.token)} for u in sample
    ]
    async with httpx.AsyncClient() as client:
        alive = await common.check_token(client, token)
        print("token alive:", alive)
        if not alive:
            raise common.TokenInvalid("token check failed")
        q1501 = await common.fetch_xts_quotes(client, token, instruments, common.MSG_TOUCHLINE)
        q1510 = await common.fetch_xts_quotes(client, token, instruments, common.MSG_OPENINTEREST)

    print("1501 parsed:", {k: common.quote_ltp_volume(v) for k, v in q1501.items()})
    print("1510 parsed:", {k: common.quote_oi(v) for k, v in q1510.items()})
    if q1501:
        print("raw 1501 sample:", json.dumps(next(iter(q1501.values())))[:900])
    if q1510:
        print("raw 1510 sample:", json.dumps(next(iter(q1510.values())))[:400])


if __name__ == "__main__":
    asyncio.run(main())
