"""greeks_snapshots table for faithful historical greeks/IV; drop dead oi_1h cagg.

Persists per-strike IV + greeks (delta/gamma/theta/vega) intraday so replay and
exports can show what was actually computed live, instead of recomputing on read
(non-deterministic). Populated going forward by a background loop — not
backfilled. Also drops the ``oi_1h`` continuous aggregate, which no code path
ever read.

Revision ID: 0004_greeks_snapshots
Revises: 0003_iv_daily
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op

revision = "0004_greeks_snapshots"
down_revision = "0003_iv_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS greeks_snapshots (
            ts          TIMESTAMPTZ      NOT NULL,
            symbol      TEXT             NOT NULL,
            expiry      DATE             NOT NULL,
            strike      INTEGER          NOT NULL,
            option_type CHAR(2)          NOT NULL,   -- 'CE' | 'PE'
            iv          DOUBLE PRECISION,
            delta       DOUBLE PRECISION,
            gamma       DOUBLE PRECISION,
            theta       DOUBLE PRECISION,
            vega        DOUBLE PRECISION,
            PRIMARY KEY (ts, symbol, expiry, strike, option_type)
        );
        """
    )
    op.execute(
        "SELECT create_hypertable('greeks_snapshots','ts', "
        "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_greeks_expiry_strike_ts "
        "ON greeks_snapshots (symbol, expiry, strike, option_type, ts DESC);"
    )
    # Columnar compression + retention (mirror option_oi_snapshots / 0002).
    op.execute(
        """
        ALTER TABLE greeks_snapshots SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol',
            timescaledb.compress_orderby = 'ts DESC'
        );
        """
    )
    op.execute("SELECT add_compression_policy('greeks_snapshots', INTERVAL '7 days');")
    op.execute("SELECT add_retention_policy('greeks_snapshots', INTERVAL '180 days');")

    # The oi_1h continuous aggregate (0001) is never read by any application code.
    # Drop it so it stops consuming refresh cycles and misleading readers.
    op.execute("DROP MATERIALIZED VIEW IF EXISTS oi_1h CASCADE;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS greeks_snapshots CASCADE;")
    # oi_1h is intentionally NOT recreated on downgrade — it was unused.
