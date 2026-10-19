"""Explicit tenant agent selection for cold direct messages.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0110_cold_dm_agent"
down_revision: str | None = "0109_github_tokens_live_index"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("direct_message_policies", sa.Column("dm_agent_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("direct_message_policies", "dm_agent_name")
