"""Record how far the turn-end billing catch-up has read each MA session.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0087_usage_reconcile_progress"
down_revision: str | None = "0086_discord_gateway_lease"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "usage_reconcile_progress",
        sa.Column("managed_session_id", sa.Text(), primary_key=True),
        sa.Column("covered_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("usage_reconcile_progress")
