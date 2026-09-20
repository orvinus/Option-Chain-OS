"""algo_trades.broker_order_id — persist the broker order id so an open LIVE
position can be re-adopted (and its exit routed to the BROKER, not paper)
after a backend restart. Before this column, a restart orphaned the position:
the orchestrator came up flat, nothing re-read the open row, and the exit
would have routed to paper even if it had.

Revision ID: 0012_trade_broker_order_id
Revises: 0011_backtest_sandbox
"""
from alembic import op

revision = "0012_trade_broker_order_id"
down_revision = "0011_backtest_sandbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE algo_trades "
        "ADD COLUMN IF NOT EXISTS broker_order_id TEXT NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE algo_trades DROP COLUMN IF EXISTS broker_order_id")
