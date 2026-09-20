"""oi_archive_bars — vendor-backfilled 1-min history + unified read view.

Holds TrueData ``getbars`` 1-min history (NIFTY + SENSEX chains, futures, index)
imported by ``scripts/truedata_backfill.py``. Kept OUT of ``option_oi_snapshots``
on purpose:

* the live table has a 180-day retention policy — six-month-old imports would sit
  at the deletion edge and be erased by the very next policy run;
* its chunks compress after 7 days, and bulk inserts into old compressed chunks
  are slow and bloat the table;
* it is the live product's single-writer store (the aggregator) — backfill must
  never race it.

``oi_snapshots_unified`` is the read-side union consumed by replay/history-dates/
expiries. No retention here — archive data is permanent. The importer guarantees
no (symbol, day) overlap with the live table, so UNION ALL never double-counts.

Revision ID: 0005_oi_archive
Revises: 0004_greeks_snapshots
Create Date: 2026-08-07
"""
from __future__ import annotations

from alembic import op

revision = "0005_oi_archive"
down_revision = "0004_greeks_snapshots"
branch_labels = None
depends_on = None

COMPRESS_AFTER = "7 days"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oi_archive_bars (
            ts          TIMESTAMPTZ      NOT NULL,
            symbol      TEXT             NOT NULL,
            expiry      DATE             NOT NULL,
            strike      BIGINT           NOT NULL,
            option_type TEXT             NOT NULL,
            token       TEXT             NOT NULL,
            open        DOUBLE PRECISION,
            high        DOUBLE PRECISION,
            low         DOUBLE PRECISION,
            close       DOUBLE PRECISION,
            volume      BIGINT,
            volume_cum  BIGINT,
            oi          BIGINT,
            underlying  DOUBLE PRECISION,
            source      TEXT NOT NULL DEFAULT 'td_getbars',
            PRIMARY KEY (ts, token)
        );
        """
    )
    op.execute(
        "SELECT create_hypertable('oi_archive_bars', 'ts', "
        "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_arch_symbol_expiry_ts "
        "ON oi_archive_bars (symbol, expiry, ts DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_arch_symbol_ts "
        "ON oi_archive_bars (symbol, ts DESC);"
    )
    op.execute(
        """
        ALTER TABLE oi_archive_bars SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol',
            timescaledb.compress_orderby = 'ts DESC'
        );
        """
    )
    op.execute(
        f"SELECT add_compression_policy('oi_archive_bars', INTERVAL '{COMPRESS_AFTER}', "
        f"if_not_exists => TRUE);"
    )
    # NO retention policy: the archive is permanent by design.

    # Tick-by-tick capture (getticks — vendor serves only the LAST 5 TRADING
    # DAYS, so this is a use-it-or-lose-it store). PK (ts, token) is last-wins
    # within a second — same semantics as the live 1s bucket store.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oi_archive_ticks (
            ts          TIMESTAMPTZ NOT NULL,
            symbol      TEXT        NOT NULL,
            expiry      DATE        NOT NULL,
            strike      BIGINT      NOT NULL,
            option_type TEXT        NOT NULL,
            token       TEXT        NOT NULL,
            ltp         DOUBLE PRECISION,
            volume      BIGINT,
            oi          BIGINT,
            bid         DOUBLE PRECISION,
            bidqty      BIGINT,
            ask         DOUBLE PRECISION,
            askqty      BIGINT,
            PRIMARY KEY (ts, token)
        );
        """
    )
    op.execute(
        "SELECT create_hypertable('oi_archive_ticks', 'ts', "
        "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_atk_symbol_expiry_ts "
        "ON oi_archive_ticks (symbol, expiry, ts DESC);"
    )
    op.execute(
        """
        ALTER TABLE oi_archive_ticks SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol',
            timescaledb.compress_orderby = 'ts DESC'
        );
        """
    )
    op.execute(
        f"SELECT add_compression_policy('oi_archive_ticks', INTERVAL '{COMPRESS_AFTER}', "
        f"if_not_exists => TRUE);"
    )

    # Daily OHLCV+OI: long-horizon index/futures dailies (vendor serves 10+
    # years of daily bars) + bhavcopy extracts for contract-level EOD.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS eod_bars (
            trade_date  DATE   NOT NULL,
            symbol      TEXT   NOT NULL,
            expiry      DATE   NOT NULL,
            strike      BIGINT NOT NULL,
            option_type TEXT   NOT NULL,
            token       TEXT   NOT NULL,
            open        DOUBLE PRECISION,
            high        DOUBLE PRECISION,
            low         DOUBLE PRECISION,
            close       DOUBLE PRECISION,
            volume      BIGINT,
            oi          BIGINT,
            source      TEXT NOT NULL DEFAULT 'td_eod',
            PRIMARY KEY (trade_date, token)
        );
        CREATE INDEX IF NOT EXISTS ix_eod_symbol_date ON eod_bars (symbol, trade_date DESC);
        """
    )

    # FII/DII daily flows (corporate host; category = INDEX FUTURES / INDEX
    # OPTIONS / STOCK FUTURES / …).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fii_dii_flows (
            trade_date DATE NOT NULL,
            category   TEXT NOT NULL,
            exchange   TEXT NOT NULL,
            buy        DOUBLE PRECISION,
            sell       DOUBLE PRECISION,
            net        DOUBLE PRECISION,
            PRIMARY KEY (trade_date, category, exchange)
        );
        """
    )

    # News archive (corporate host getNewsForDateRange; 4 publishers).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS news_items (
            id          TEXT PRIMARY KEY,
            pub_date    TIMESTAMPTZ,
            title       TEXT,
            description TEXT,
            category    TEXT,
            source      TEXT,
            source_link TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_news_pub_date ON news_items (pub_date DESC);
        """
    )

    # Importer checkpoint ledger — one row per fetch unit; re-runs skip 'done'.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oi_backfill_progress (
            unit_key   TEXT PRIMARY KEY,
            status     TEXT NOT NULL DEFAULT 'pending',
            rows       INTEGER NOT NULL DEFAULT 0,
            error      TEXT,
            fetched_at TIMESTAMPTZ
        );
        """
    )

    # Read-side union with the live table's column shape. IDX/FUT context rows
    # (strike 0) are excluded: consumers expect CE/PE books, and the live table
    # never stores spot rows (spot rides denormalized in ``underlying``).
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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS oi_snapshots_unified;")
    op.execute("DROP TABLE IF EXISTS oi_backfill_progress;")
    op.execute("DROP TABLE IF EXISTS news_items;")
    op.execute("DROP TABLE IF EXISTS fii_dii_flows;")
    op.execute("DROP TABLE IF EXISTS eod_bars;")
    op.execute("DROP TABLE IF EXISTS oi_archive_ticks CASCADE;")
    op.execute("DROP TABLE IF EXISTS oi_archive_bars CASCADE;")
