"""Algo Config foundation: versioned config store, users, audit log, signals, trades.

Five tables backing the semi-automated trading engine (the "Algo Config" tab):

- ``algo_config_versions`` — the ENTIRE configuration document (global + 5 days +
  15 zones + per-zone engine parameters) as one JSONB row per save. The live
  config is simply ``MAX(version)``. This gives the spec's two requirements in
  one shape: §13's atomic confirm-then-commit (a save is one INSERT — there is
  no partially-saved zone state to crash into) and §11/§6's Version History /
  Restore panel (restore = re-insert an old document as a new version, so the
  audit trail never rewinds).
- ``algo_users`` — one row per person, never shared (§11.1). Passwords are
  PBKDF2-HMAC-SHA256, per-user salt; only the admin is seeded for now, but the
  role column already carries admin|editor|viewer for the later RBAC build.
- ``algo_audit_log`` — append-only (§11.3): every login success AND failure,
  every config change as old→new per field, kills, safety-gate blocks, trade
  events. No UPDATE/DELETE path exists in application code; a correction is a
  new row.
- ``algo_signals`` — engine output transitions (per zone, per indicator, plus
  the combined unanimous result), payload JSON included so a logged signal can
  be replayed/explained later. Deliberately a PLAIN table, not a hypertable:
  volume is a handful of rows per zone per day (signal *transitions*, not every
  evaluation), so chunking overhead buys nothing.
- ``algo_trades`` — the append-only trade ledger with the §12.4 required fields
  and the live/paper ledger tag that keeps the two from ever mixing in a query
  by accident.

Revision ID: 0008_algo_foundation
Revises: 0007_archive_source_provenance
Create Date: 2026-08-13
"""
from __future__ import annotations

from alembic import op

revision = "0008_algo_foundation"
down_revision = "0007_archive_source_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_config_versions (
            version     SERIAL PRIMARY KEY,
            config      JSONB NOT NULL,
            saved_by    TEXT NOT NULL DEFAULT '',
            saved_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            note        TEXT NOT NULL DEFAULT ''
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_users (
            user_id       SERIAL PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'admin'
                          CHECK (role IN ('admin', 'editor', 'viewer')),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_audit_log (
            id          BIGSERIAL PRIMARY KEY,
            ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            user_id     INTEGER NULL REFERENCES algo_users(user_id),
            username    TEXT NOT NULL DEFAULT '',
            event_type  TEXT NOT NULL,
            scope       TEXT NOT NULL DEFAULT '',
            field       TEXT NOT NULL DEFAULT '',
            old_value   TEXT NULL,
            new_value   TEXT NULL,
            detail      JSONB NULL
        );
        CREATE INDEX IF NOT EXISTS idx_algo_audit_ts ON algo_audit_log (ts DESC);
        CREATE INDEX IF NOT EXISTS idx_algo_audit_user_ts ON algo_audit_log (username, ts DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_signals (
            id          BIGSERIAL PRIMARY KEY,
            ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            trade_date  DATE NOT NULL,
            day         TEXT NOT NULL,
            zone_id     TEXT NOT NULL,
            indicator   TEXT NOT NULL,
            reading     TEXT NOT NULL,
            payload     JSONB NULL
        );
        CREATE INDEX IF NOT EXISTS idx_algo_signals_date_zone
            ON algo_signals (trade_date, zone_id, ts DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_trades (
            id            BIGSERIAL PRIMARY KEY,
            trade_date    DATE NOT NULL,
            day           TEXT NOT NULL,
            zone_id       TEXT NOT NULL,
            index_symbol  TEXT NOT NULL,
            side          TEXT NOT NULL CHECK (side IN ('CALL', 'PUT')),
            token         TEXT NOT NULL DEFAULT '',
            strike        INTEGER NULL,
            expiry        DATE NULL,
            entry_ts      TIMESTAMPTZ NOT NULL,
            entry_price   NUMERIC(14,2) NOT NULL,
            exit_ts       TIMESTAMPTZ NULL,
            exit_price    NUMERIC(14,2) NULL,
            lots          INTEGER NOT NULL,
            pnl_rupees    NUMERIC(14,2) NULL,
            pnl_pct       NUMERIC(10,4) NULL,
            exit_reason   TEXT NOT NULL DEFAULT '',
            ledger        TEXT NOT NULL CHECK (ledger IN ('live', 'paper')),
            sub_scenario  TEXT NOT NULL DEFAULT '',
            fees          JSONB NULL
        );
        CREATE INDEX IF NOT EXISTS idx_algo_trades_date
            ON algo_trades (trade_date DESC, ledger);
        CREATE INDEX IF NOT EXISTS idx_algo_trades_zone
            ON algo_trades (zone_id, trade_date DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS algo_trades;")
    op.execute("DROP TABLE IF EXISTS algo_signals;")
    op.execute("DROP TABLE IF EXISTS algo_audit_log;")
    op.execute("DROP TABLE IF EXISTS algo_users;")
    op.execute("DROP TABLE IF EXISTS algo_config_versions;")
