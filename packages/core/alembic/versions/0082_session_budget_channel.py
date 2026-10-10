"""Retain the immutable budget-channel stamp on thread mappings.

Revision ID: 0082_session_budget_channel
Revises: 0081_skill_add_one_preview

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0082_session_budget_channel"
down_revision: str | None = "0081_skill_add_one_preview"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.get_bind().execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.add_column(
        "thread_sessions",
        sa.Column("budget_channel_id", sa.Text(), nullable=True, server_default=""),
    )
    op.alter_column("thread_sessions", "budget_channel_id", server_default=None)


def downgrade() -> None:
    op.get_bind().execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.drop_column("thread_sessions", "budget_channel_id")
