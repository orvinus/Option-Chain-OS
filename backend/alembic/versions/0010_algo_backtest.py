"""Backtest storage — runs, trades, per-day audit, signal/event timeline.

Why dedicated tables instead of a third ledger inside ``algo_trades``:

- ``algo_trades`` is append-only by design (its only sanctioned delete is the
  paper reset) and carries ``CHECK (ledger IN ('live','paper'))``. A backtest
  needs *wholesale deletion of a run* — one click, no live/paper rows at risk.
- ``algo_signals`` has no run scoping — 6 months of simulated transitions
  would pollute the live signal log irreversibly.
- The default trade-log endpoint treats ``ledger=''`` as "all ledgers"; a
  third value would silently leak simulated trades into the live views.

``algo_backtest_trades`` mirrors the exact ``algo_trades`` column shape (see
0008) so the P&L/trade-log serialization is copy-paste reusable. All children
cascade on run deletion so cleanup is a single DELETE. ``cursor_date`` is
deliberately not named ``current_date`` (reserved word).
"""
from alembic import op

revision = "0010_algo_backtest"
down_revision = "0009_day_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_backtest_runs (
            id             BIGSERIAL PRIMARY KEY,
            label          TEXT NOT NULL DEFAULT '',
            created_by     TEXT NOT NULL DEFAULT '',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            from_date      DATE NOT NULL,
            to_date        DATE NOT NULL,
            config         JSONB NOT NULL,
            config_version INTEGER NULL,
            settings       JSONB NOT NULL DEFAULT '{}'::jsonb,
            status         TEXT NOT NULL DEFAULT 'queued'
                           CHECK (status IN ('queued','running','done','error','cancelled')),
            days_total     INTEGER NOT NULL DEFAULT 0,
            days_done      INTEGER NOT NULL DEFAULT 0,
            cursor_date    DATE NULL,
            started_at     TIMESTAMPTZ NULL,
            finished_at    TIMESTAMPTZ NULL,
            summary        JSONB NULL,
            error          TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS algo_backtest_trades (
            id            BIGSERIAL PRIMARY KEY,
            run_id        BIGINT NOT NULL REFERENCES algo_backtest_runs(id) ON DELETE CASCADE,
            seq           INTEGER NOT NULL,
            trade_date    DATE NOT NULL,
            day           TEXT NOT NULL,
            zone_id       TEXT NOT NULL,
            index_symbol  TEXT NOT NULL,
            side          TEXT NOT NULL CHECK (side IN ('CALL','PUT')),
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
            ledger        TEXT NOT NULL CHECK (ledger IN ('live','paper')),
            sub_scenario  TEXT NOT NULL DEFAULT '',
            fees          JSONB NULL
        );
        CREATE INDEX IF NOT EXISTS idx_abt_trades_run
            ON algo_backtest_trades (run_id, trade_date, entry_ts);

        CREATE TABLE IF NOT EXISTS algo_backtest_days (
            run_id      BIGINT NOT NULL REFERENCES algo_backtest_runs(id) ON DELETE CASCADE,
            trade_date  DATE NOT NULL,
            status      TEXT NOT NULL DEFAULT 'planned'
                        CHECK (status IN ('planned','skipped','done','error')),
            skip_reason TEXT NOT NULL DEFAULT '',
            detail      JSONB NULL,
            PRIMARY KEY (run_id, trade_date)
        );

        CREATE TABLE IF NOT EXISTS algo_backtest_signals (
            id         BIGSERIAL PRIMARY KEY,
            run_id     BIGINT NOT NULL REFERENCES algo_backtest_runs(id) ON DELETE CASCADE,
            ts         TIMESTAMPTZ NOT NULL,
            trade_date DATE NOT NULL,
            day        TEXT NOT NULL,
            zone_id    TEXT NOT NULL DEFAULT '',
            indicator  TEXT NOT NULL,
            reading    TEXT NOT NULL,
            payload    JSONB NULL
        );
        CREATE INDEX IF NOT EXISTS idx_abt_signals_run
            ON algo_backtest_signals (run_id, trade_date, ts);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS algo_backtest_signals;
        DROP TABLE IF EXISTS algo_backtest_days;
        DROP TABLE IF EXISTS algo_backtest_trades;
        DROP TABLE IF EXISTS algo_backtest_runs;
        """
    )
