"""One-shot ScripMaster refresh.

Run nightly (e.g. via Windows Task Scheduler at 8am IST) to ensure the cache
is hot before market open.  Independent of the backend process.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.market.scripmaster import get_scripmaster  # noqa: E402  (import after sys.path tweak)


async def main() -> None:
    rows = await get_scripmaster(force_refresh=True)
    print(f"Refreshed scripmaster: {len(rows):,} instruments cached.")


if __name__ == "__main__":
    asyncio.run(main())
