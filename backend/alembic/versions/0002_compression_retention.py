"""timescale compression + retention on option_oi_snapshots

The universe poller multiplies row volume (~all F&O symbols, ~millions of rows/day).
Enable native TimescaleDB columnar compression on chunks older than 7 days and a
180-day retention policy so raw storage stays bounded. The oi_1h continuous
aggregate (start_offset 7 days) materialises well before compression/retention
touch a chunk, so hourly history survives retention of the raw rows.

Tunable in prod via SQL without a migration:
    SELECT remove_compression_policy('option_oi_snapshots');
    SELECT add_compression_policy('option_oi_snapshots', INTERVAL '3 days');
    SELECT remove_retention_policy('option_oi_snapshots');
    SELECT add_retention_policy('option_oi_snapshots', INTERVAL '365 days');

Revision ID: 0002_compression_retention
Revises: 0001_init
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op

revision = "0002_compression_retention"
down_revision = "0001_init"
branch_labels = None
depends_on = None

COMPRESS_AFTER = "7 days"
RETAIN_FOR = "180 days"


def upgrade() -> None:
    # Enable columnar compression. Segment by symbol (moderate cardinality → good
    # ratios and symbol-scoped scans); order by ts DESC within a segment.
    op.execute(
        """
        ALTER TABLE option_oi_snapshots SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol',
            timescaledb.compress_orderby = 'ts DESC'
        );
        """
    )
    op.execute(
        f"SELECT add_compression_policy('option_oi_snapshots', INTERVAL '{COMPRESS_AFTER}', "
        f"if_not_exists => TRUE);"
    )
    op.execute(
        f"SELECT add_retention_policy('option_oi_snapshots', INTERVAL '{RETAIN_FOR}', "
        f"if_not_exists => TRUE);"
    )


def downgrade() -> None:
    # Best-effort: drop policies. Compressed chunks are left as-is (decompress
    # manually before disabling compression if you truly need to revert).
    op.execute("SELECT remove_retention_policy('option_oi_snapshots', if_exists => TRUE);")
    op.execute("SELECT remove_compression_policy('option_oi_snapshots', if_exists => TRUE);")
