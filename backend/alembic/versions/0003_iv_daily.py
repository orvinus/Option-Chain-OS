"""iv_daily table for ATM IV history (IVR / IVP / 1yr range)

Revision ID: 0003_iv_daily
Revises: 0002_compression_retention
Create Date: 2026-07-16
"""
from __future__ import annotations

from alembic import op

revision = "0003_iv_daily"
down_revision = "0002_compression_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS iv_daily (
            symbol      TEXT             NOT NULL,
            trade_date  DATE             NOT NULL,
            atm_iv      DOUBLE PRECISION NOT NULL,
            spot        NUMERIC(14, 2),
            expiry      DATE,
            created_at  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
            PRIMARY KEY (symbol, trade_date)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_iv_daily_symbol_date "
        "ON iv_daily (symbol, trade_date DESC);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS iv_daily;")
