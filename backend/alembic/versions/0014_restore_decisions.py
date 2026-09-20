"""Same-day session restore provenance, data-integrity runs, and the
per-minute decision trace tables (live + backtest).

1. ``option_oi_snapshots.src`` (nullable SMALLINT): NULL = row written by the
   live feed; 1 = minute restored from the vendor's same-day archive by the
   session catch-up (``ingest/gapfill.py``) because the feed had no row for
   that token in that minute. Metadata-only ALTER (nullable, no default) —
   allowed on a compressed hypertable. The premium path excludes ``src=1``
   rows from its live arm and reads the archive's true O/H/L/C instead.
2. ``data_integrity_runs``: one persisted report per (symbol, IST day).
3. ``algo_decisions`` / ``algo_backtest_decisions``: one row per evaluated
   minute per zone (and the candidate strikes in it) — the filter-by-filter
   record that did not exist anywhere before. Two tables on purpose, like
   0010: simulated rows never pollute the live table and cascade with the run.

Revision ID: 0014_restore_decisions
Revises: 0013_data_health
"""
from alembic import op

revision = "0014_restore_decisions"
down_revision = "0013_data_health"
branch_labels = None
depends_on = None

_DECISION_COLUMNS = """
    ts               TIMESTAMPTZ NOT NULL,
    trade_date       DATE        NOT NULL,
    day              TEXT        NOT NULL DEFAULT '',
    zone_id          TEXT        NOT NULL DEFAULT '',
    symbol           TEXT        NOT NULL DEFAULT '',
    expiry           DATE,
    ledger           TEXT        NOT NULL DEFAULT '',
    config_version   INTEGER,
    stage            TEXT        NOT NULL,
    state            TEXT        NOT NULL DEFAULT '',
    direction        TEXT        NOT NULL DEFAULT '',
    unanimous        BOOLEAN,
    decision         TEXT        NOT NULL,
    reason           TEXT        NOT NULL DEFAULT '',
    gate_blocks      JSONB,
    readings         JSONB,
    candidates       JSONB,
    sizing           JSONB,
    position         JSONB,
    zone_snapshot    JSONB,
    data_age_s       DOUBLE PRECISION,
    trade_id         BIGINT
"""


def upgrade() -> None:
    op.execute("ALTER TABLE option_oi_snapshots ADD COLUMN IF NOT EXISTS src SMALLINT")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS data_integrity_runs (
            symbol        TEXT        NOT NULL,
            day           DATE        NOT NULL,
            generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            report        JSONB       NOT NULL,
            PRIMARY KEY (symbol, day)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS algo_decisions (
            id BIGSERIAL PRIMARY KEY,
            {_DECISION_COLUMNS}
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_algo_decisions_day "
        "ON algo_decisions (trade_date, zone_id, ts)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS algo_backtest_decisions (
            id     BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES algo_backtest_runs(id) ON DELETE CASCADE,
            {_DECISION_COLUMNS}
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_abt_decisions_run "
        "ON algo_backtest_decisions (run_id, trade_date, ts)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS algo_backtest_decisions")
    op.execute("DROP TABLE IF EXISTS algo_decisions")
    op.execute("DROP TABLE IF EXISTS data_integrity_runs")
    # Restored rows are identifiable; remove them before dropping the marker so
    # the live table holds only feed-written rows again.
    op.execute("DELETE FROM option_oi_snapshots WHERE src = 1")
    op.execute("ALTER TABLE option_oi_snapshots DROP COLUMN IF EXISTS src")
