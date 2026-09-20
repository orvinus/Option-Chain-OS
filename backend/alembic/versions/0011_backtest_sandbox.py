"""Backtest sandbox config + broker account snapshot.

``algo_backtest_sandbox`` holds the ONE editable configuration document the
standalone Backtesting workspace edits — deliberately OUTSIDE
``algo_config_versions`` so nothing done in the workspace can ever change
what the live orchestrator trades (live = MAX(version) of that table).
Runs launched from the sandbox freeze the document into the run row
(source=inline), so past runs stay reproducible even as the sandbox evolves.

``algo_broker_snapshot`` caches the last SUCCESSFUL broker-account payload so
the Integrations panel can show funds/positions/orders ("as of Friday 15:29")
while the vendor gateway is offline — weekends previously rendered a raw
HTML 404 and nothing else.
"""
from alembic import op

revision = "0011_backtest_sandbox"
down_revision = "0010_algo_backtest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_backtest_sandbox (
            id          BIGSERIAL PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE DEFAULT 'sandbox',
            config      JSONB NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT '',
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS algo_broker_snapshot (
            id          SMALLINT PRIMARY KEY CHECK (id = 1),
            payload     JSONB NOT NULL,
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS algo_broker_snapshot;
        DROP TABLE IF EXISTS algo_backtest_sandbox;
        """
    )
