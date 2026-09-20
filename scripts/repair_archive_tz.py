"""Repair the 23-minute timestamp shift in the TrueData archive.

WHAT WENT WRONG
``scripts/truedata_backfill.py`` converted the vendor's IST-naive bar stamps with
``naive.replace(tzinfo=IST)``. ``IST`` is a pytz zone, and pytz tzinfo objects
carry every historical offset the zone has ever had; ``replace`` selects the
FIRST, which for Asia/Kolkata is Local Mean Time at **+05:53** rather than
+05:30. Every row written by the backfill therefore landed **23 minutes early**.

No error, no warning, and a completely plausible-looking value — which is why it
survived six months of collection and a validation pass.

HOW IT WAS FOUND (2026-08-13)
By joining ``oi_archive_bars`` to ``option_oi_snapshots`` for the same contracts
and minutes at a range of offsets, and comparing OI:

    offset   0 min : 10,011 pairs,  11.24% agreement
    offset -23 min : 10,539 pairs,  92.15% agreement     <-- the truth

Corroborated by the stored session bounds: the archive ran 08:44-15:07 IST where
the market runs 09:07-15:30, i.e. exactly 23 minutes early at both ends.

WHAT IT AFFECTED
Every consumer of ``oi_snapshots_unified`` on archive-only dates — Replay,
Charts, Multi-TF and Ratio — displayed data shifted 23 minutes earlier than it
actually occurred. Live-table rows were never affected: the live feed stamps
arrival time with ``datetime.now(timezone.utc)`` and never touches pytz.

WHAT THIS SCRIPT DOES
Shifts affected rows forward by exactly 23 minutes, keyed on PROVENANCE rather
than on any property of the timestamps:

    source = 'td_getbars'      written by the broken converter  -> shift
    source = 'td_getbars_v2'   written by the fixed converter   -> leave alone
    source = 'td_getbars_tzfixed'  already repaired by this script -> leave alone

Provenance, not a heuristic, because heuristics get this wrong in both
directions. A first attempt classified days by their last IST stamp and missed
two of 128 — one because the exchange changed its closing time mid-series, one
because the day was still in progress. And getting it wrong is expensive: a
double application was reproduced on a scratch hypertable, moving 14:15 IST to
14:38. With the marker, re-running is exactly a no-op.

    python scripts/repair_archive_tz.py --dry-run     # always start here
    python scripts/repair_archive_tz.py --apply

Requires migration 0007 (adds ``source`` to oi_archive_ticks). Take a backup
first (``oi-backup`` on the VPS): this rewrites the time column of a hypertable.
Verified against compressed chunks — the UPDATE succeeds and the chunk stays
compressed — but it is still a bulk rewrite of 24M rows, so run it market-closed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402

# +05:53 (pytz LMT) minus +05:30 (real IST).
SHIFT_MINUTES = 23

# table -> the source value the BROKEN writer left behind.
TABLES = {
    "oi_archive_bars": "td_getbars",
    "oi_archive_ticks": "td_getticks",
}
FIXED_SUFFIX = "_tzfixed"

_AUDIT_SQL = """
SELECT source,
       count(*)                                          AS rows,
       min((ts AT TIME ZONE 'Asia/Kolkata')::time)       AS first_ist,
       max((ts AT TIME ZONE 'Asia/Kolkata')::time)       AS last_ist,
       count(DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date) AS days
FROM {table}
GROUP BY source
ORDER BY rows DESC
"""

# Shift and re-stamp in ONE statement. Doing them separately would leave a window
# where a crash produced shifted-but-unmarked rows — which the next run would
# shift again, silently, with no way to tell from the data that it had happened.
#
# The predicate is a HALF-OPEN RANGE ON ts, not `(ts AT TIME ZONE ...)::date = :day`.
# That distinction is not cosmetic: a function over ts cannot use the time index
# or chunk exclusion, so Timescale decompressed the ENTIRE hypertable for a single
# day's update — measured at 14,944,080 tuples for one day, which then blew the
# `max_tuples_decompressed_per_dml_transaction` limit of 100,000 and aborted.
# A plain UPDATE CANNOT do this. The primary key is (ts, token), and shifting a
# row from T to T+23 collides with the row that is still sitting at T+23 — the
# whole set has to move at once, but UPDATE applies row by row:
#
#   UniqueViolation: Key (ts, token)=(2026-02-09 03:45:00+00, td:SENSEX:...) already exists
#
# So: copy the range out, shift the copy, delete the originals, insert them back.
# `SELECT *` keeps this schema-agnostic across the bars and ticks tables, and the
# temp table is ON COMMIT DROP so a failure leaves nothing behind.
#
# ON CONFLICT DO NOTHING covers the one legitimate overlap: if the FIXED backfill
# has already re-fetched a minute, its td_*_v2 row is authoritative and the
# shifted legacy row is a duplicate of it. Dropping the legacy row is correct —
# and the count is reported, never silent.
_SWAP_SQL = """
CREATE TEMP TABLE _shift ON COMMIT DROP AS
    SELECT * FROM {table}
    WHERE source = :broken AND ts >= :lo AND ts < :hi;

UPDATE _shift SET ts = ts + INTERVAL '{shift} minutes', source = :fixed;

DELETE FROM {table}
 WHERE source = :broken AND ts >= :lo AND ts < :hi;

INSERT INTO {table} SELECT * FROM _shift
    ON CONFLICT (ts, token) DO NOTHING;
"""

# Work chunk by chunk. Each chunk is one day, so the transaction stays bounded and
# a failure costs one chunk rather than the whole table.
_CHUNKS_SQL = """
SELECT c.chunk_schema || '.' || c.chunk_name AS chunk,
       c.range_start, c.range_end, c.is_compressed
FROM timescaledb_information.chunks c
WHERE c.hypertable_name = :table
ORDER BY c.range_start
"""

# A +23 minute shift never moves a row across a UTC midnight — the archived
# session spans roughly 02:34-09:53 UTC — so rows stay inside their own chunk and
# no cross-chunk migration is involved.
_COUNT_IN_RANGE_SQL = """
SELECT count(*) FROM {table}
WHERE source = :broken AND ts >= :lo AND ts < :hi
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="perform the update")
    ap.add_argument("--dry-run", action="store_true", default=True)
    args = ap.parse_args()
    apply = bool(args.apply)

    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    total_rows = 0
    try:
        for table, broken in TABLES.items():
            fixed = broken + FIXED_SUFFIX
            async with engine.connect() as conn:
                try:
                    audit = (await conn.execute(
                        text(_AUDIT_SQL.format(table=table))
                    )).mappings().all()
                except Exception as e:
                    print(f"== {table}: skipped ({str(e)[:110]})")
                    continue

            print(f"\n== {table}")
            for r in audit:
                mark = "  <- SHIFT" if r["source"] == broken else ""
                print(f"   source={r['source']:<24s} rows={r['rows']:>12,} "
                      f"days={r['days']:>4}  IST {r['first_ist']}..{r['last_ist']}{mark}")

            todo = next((r for r in audit if r["source"] == broken), None)
            if todo is None:
                print("   nothing to repair")
                continue
            total_rows += int(todo["rows"])
            if not apply:
                continue

            async with engine.connect() as conn:
                chunks = (await conn.execute(
                    text(_CHUNKS_SQL), {"table": table}
                )).mappings().all()

            done = 0
            for i, ch in enumerate(chunks, 1):
                lo, hi, name, was_compressed = (
                    ch["range_start"], ch["range_end"], ch["chunk"], ch["is_compressed"]
                )
                async with engine.connect() as conn:
                    n = (await conn.execute(
                        text(_COUNT_IN_RANGE_SQL.format(table=table)),
                        {"broken": broken, "lo": lo, "hi": hi},
                    )).scalar_one()
                if not n:
                    continue

                # DECOMPRESS -> UPDATE -> RECOMPRESS.
                #
                # Updating a compressed chunk in place is supported but pathological
                # here: Timescale decompresses row-by-row inside the transaction and
                # trips max_tuples_decompressed_per_dml_transaction (default 100k).
                # Explicitly decompressing first is the documented pattern, is far
                # faster, and keeps each transaction small.
                async with engine.begin() as conn:
                    if was_compressed:
                        await conn.execute(text(f"SELECT decompress_chunk('{name}')"))
                    params = {"broken": broken, "fixed": fixed, "lo": lo, "hi": hi}
                    inserted = 0
                    for stmt in _SWAP_SQL.format(table=table, shift=SHIFT_MINUTES).split(";"):
                        if stmt.strip():
                            r = await conn.execute(text(stmt), params)
                            if stmt.strip().upper().startswith("INSERT"):
                                inserted = r.rowcount
                if was_compressed:
                    # Recompress OUTSIDE the swap transaction: compression takes its
                    # own locks, and holding both makes a long stall likelier.
                    async with engine.begin() as conn:
                        await conn.execute(text(f"SELECT compress_chunk('{name}')"))
                done += inserted
                dropped = n - inserted
                print(f"   [{i:>3}/{len(chunks)}] {str(lo)[:10]}: {inserted:,} rows "
                      f"+{SHIFT_MINUTES}min"
                      + (f", {dropped:,} superseded by v2" if dropped else "")
                      + (" (recompressed)" if was_compressed else ""))
            print(f"   {table}: {done:,} rows shifted")
    finally:
        await engine.dispose()

    print(f"\n{'APPLIED' if apply else 'DRY RUN'}: {total_rows:,} rows "
          f"{'shifted' if apply else 'would be shifted'} by +{SHIFT_MINUTES} min")
    if not apply and total_rows:
        print("\nRe-run with --apply to perform the repair. Back up first:")
        print("   ssh root@oialgo.tech 'oi-backup'")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
