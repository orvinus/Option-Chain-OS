"""Provenance column on oi_archive_ticks, so the timezone repair is idempotent.

``oi_archive_bars`` already has a ``source`` column; ``oi_archive_ticks`` does
not. Both need one, because ``scripts/repair_archive_tz.py`` has to distinguish
rows written by the BROKEN backfill (IST-naive stamps converted with pytz's
+05:53 Local Mean Time, i.e. 23 minutes early) from rows written by the fixed
one — and re-running the repair on already-repaired rows would shift them a
second time. That failure mode is not theoretical: it was reproduced on a scratch
hypertable, where an unguarded second run moved 14:15 IST to 14:38.

This migration only adds the column. The data repair itself is deliberately NOT
here: migrations run as part of the container's start command
(``alembic upgrade head && uvicorn``), and a 24-million-row UPDATE in that path
would delay every boot and, on failure, prevent the backend from starting at all.
The repair is a separately-invoked, resumable, dry-runnable script.

Revision ID: 0007_archive_source_provenance
Revises: 0006_td_shadow_and_overlap_fix
Create Date: 2026-08-13
"""
from __future__ import annotations

from alembic import op

revision = "0007_archive_source_provenance"
down_revision = "0006_td_shadow_and_overlap_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE oi_archive_ticks "
        "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'td_getticks';"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE oi_archive_ticks DROP COLUMN IF EXISTS source;")
