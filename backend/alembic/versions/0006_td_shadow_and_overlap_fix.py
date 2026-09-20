"""TrueData shadow store, contract identity, and the archive-overlap fix.

Three independent pieces, all prerequisites for running a TrueData live feed
beside the XTS one.

1. ``td_shadow_snapshots`` — where the shadow feed writes.

   The obvious approach (both feeds into ``option_oi_snapshots`` under different
   token namespaces) is UNSAFE in this codebase. Every live hot-path query keys
   on ``(symbol, expiry, strike, option_type)`` and takes ``DISTINCT ON (strike,
   option_type) ... ORDER BY ts DESC``; ``token`` appears in no WHERE clause and
   no DISTINCT ON anywhere — services/oi_change.py, services/option_chain_full.py,
   api/option_chain.py, api/verify_option_chain.py, services/spot_fallback.py,
   services/iv_scanner.py, ws/oi_stream.py. A shadow row therefore lands on the
   user's chart the instant it is the freshest row for its strike, and flows
   onward into ``iv_daily`` (PK (symbol, trade_date), no token, no retention —
   a poisoned day is unrecoverable and skews IVR/IVP for a year).

   A separate table makes isolation provable with one grep instead of an audit of
   twenty WHERE clauses that any future refactor could widen. It also makes
   rollback ``DROP TABLE``, and it carries two columns that cannot be ALTERed onto
   a compressed 180-day hypertable: ``vendor_ts`` and ``recv_lag_ms``, which are
   the only way to measure feed latency (the live table stamps arrival time, not
   exchange time).

2. ``contract_key()`` — the vendor-neutral contract identity.

   ``'td:' || contract_key(...)`` reproduces the archive token built at
   scripts/truedata_backfill.py exactly, and ``substr(token, 4)`` reverses it, so
   cross-vendor joins need no mapping TABLE. Deliberately NOT an
   ``instrument_identity`` table: cutover-day continuity does not need one, because
   every baseline query is floored to the current session and grouped by
   (strike, option_type) — the two token families are never joined.

3. ``live_days`` + a corrected ``oi_snapshots_unified``.

   0005 asserts "the importer guarantees no (symbol, day) overlap, so UNION ALL
   never double-counts". That invariant is already violated in production:
   scripts/nightly_topup_vps.sh runs ``pull --include-live-days`` nightly for
   NIFTY and SENSEX, which disables the day-skip, and the pull's own docstring
   says callers must delete the inferior rows afterwards — the script deletes
   nothing. With PERSIST_BUCKET=1s both vendors land on exact ``ts`` ties, so
   ``last(oi, ts)`` and ``DISTINCT ON ... ORDER BY ts`` in replay/timeseries
   resolve them arbitrarily. This makes the live arm win on any day it has data.

   The day index is a MATERIALIZED view refreshed nightly, not a correlated
   subquery against the 1s hypertable: four stacked DISTINCT scans of the union
   exhausted the DB pool and starved the aggregator on 2026-08-11 (see the note in
   backend/app/api/expiries.py).

Revision ID: 0006_td_shadow_and_overlap_fix
Revises: 0005_oi_archive
Create Date: 2026-08-13
"""
from __future__ import annotations

from alembic import op

revision = "0006_td_shadow_and_overlap_fix"
down_revision = "0005_oi_archive"
branch_labels = None
depends_on = None

# Shorter than the live table's 7 days: shadow chunks are read constantly by the
# cross-vendor scorecard for ~2 days and then never again.
SHADOW_COMPRESS_AFTER = "3 days"
# Shadow data has no long-term value, and must never sit at the deletion edge of
# the real table (which is where 6-month-old rows would land under 180 days).
SHADOW_RETENTION = "45 days"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. The shadow store
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS td_shadow_snapshots (
            ts          TIMESTAMPTZ   NOT NULL,
            symbol      TEXT          NOT NULL,
            expiry      DATE          NOT NULL,
            strike      INTEGER       NOT NULL,
            option_type CHAR(2)       NOT NULL,
            token       TEXT          NOT NULL,
            oi          BIGINT        NOT NULL,
            ltp         NUMERIC(14,2),
            volume      BIGINT,
            underlying  NUMERIC(14,2),
            -- The vendor's own stamp, and arrival minus vendor. The live table has
            -- neither: ws_client stamps datetime.now() and discards the exchange
            -- time, which is why the platform has no latency baseline at all.
            vendor_ts   TIMESTAMPTZ,
            recv_lag_ms INTEGER,
            PRIMARY KEY (ts, token)
        );
        """
    )
    op.execute(
        "SELECT create_hypertable('td_shadow_snapshots', 'ts', "
        "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);"
    )
    # Indexed on the contract tuple, not the token: every cross-vendor join is
    # (symbol, expiry, strike, option_type) + nearest ts.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tds_contract_ts "
        "ON td_shadow_snapshots (symbol, expiry, strike, option_type, ts DESC);"
    )
    op.execute(
        """
        ALTER TABLE td_shadow_snapshots SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol',
            timescaledb.compress_orderby = 'ts DESC'
        );
        """
    )
    op.execute(
        f"SELECT add_compression_policy('td_shadow_snapshots', "
        f"INTERVAL '{SHADOW_COMPRESS_AFTER}', if_not_exists => TRUE);"
    )
    op.execute(
        f"SELECT add_retention_policy('td_shadow_snapshots', "
        f"INTERVAL '{SHADOW_RETENTION}', if_not_exists => TRUE);"
    )

    # ------------------------------------------------------------------
    # 2. Contract identity
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION contract_key(
            sym TEXT, exp DATE, strk BIGINT, ot TEXT
        ) RETURNS TEXT
        LANGUAGE sql IMMUTABLE STRICT AS
        $$ SELECT sym || ':' || to_char(exp, 'YYMMDD') || ':' || strk::TEXT || ':' || ot $$;
        """
    )

    # ------------------------------------------------------------------
    # 3. Overlap resolution
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE MATERIALIZED VIEW IF NOT EXISTS live_days AS
        SELECT DISTINCT symbol, (ts AT TIME ZONE 'Asia/Kolkata')::date AS day
        FROM option_oi_snapshots;
        """
    )
    # UNIQUE index is required for REFRESH ... CONCURRENTLY, which is what keeps
    # the nightly refresh from locking readers out mid-session.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_live_days ON live_days (symbol, day);"
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW oi_snapshots_unified AS
        SELECT ts, symbol, expiry, strike, option_type, token,
               oi, ltp, volume, underlying
        FROM option_oi_snapshots
        UNION ALL
        SELECT a.ts, a.symbol, a.expiry, a.strike, a.option_type, a.token,
               a.oi, a.close AS ltp, a.volume_cum AS volume, a.underlying
        FROM oi_archive_bars a
        WHERE a.option_type IN ('CE', 'PE')
          -- The live arm wins whole days. Archive rows for a day the live feed
          -- also covered are backfill duplicates, not extra information.
          AND NOT EXISTS (
              SELECT 1 FROM live_days d
              WHERE d.symbol = a.symbol
                AND d.day = (a.ts AT TIME ZONE 'Asia/Kolkata')::date
          );
        """
    )


def downgrade() -> None:
    # Restore 0005's view before dropping what it would then depend on.
    op.execute(
        """
        CREATE OR REPLACE VIEW oi_snapshots_unified AS
        SELECT ts, symbol, expiry, strike, option_type, token,
               oi, ltp, volume, underlying
        FROM option_oi_snapshots
        UNION ALL
        SELECT ts, symbol, expiry, strike, option_type, token,
               oi, close AS ltp, volume_cum AS volume, underlying
        FROM oi_archive_bars
        WHERE option_type IN ('CE', 'PE');
        """
    )
    op.execute("DROP MATERIALIZED VIEW IF EXISTS live_days;")
    op.execute("DROP FUNCTION IF EXISTS contract_key(TEXT, DATE, BIGINT, TEXT);")
    op.execute("DROP TABLE IF EXISTS td_shadow_snapshots CASCADE;")
