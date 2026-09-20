"""Per-day trading notes for the P&L calendar.

One free-text note per calendar date, written from the Algo Config P&L view
("what happened today / why I killed Z2 / expiry-day chop" — the trader's
journal line for that day). Deliberately NOT part of the config document:
notes change daily and would otherwise mint a new config version per edit,
drowning the version history the §13 save workflow depends on.

A note is UPSERTed by date; saving an empty note deletes the row (the same
sanctioned-delete pattern as the paper-ledger reset — every other algo table
stays append-only).
"""
from __future__ import annotations

from alembic import op

revision = "0009_day_notes"
down_revision = "0008_algo_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_day_notes (
            note_date  DATE PRIMARY KEY,
            note       TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS algo_day_notes;")
