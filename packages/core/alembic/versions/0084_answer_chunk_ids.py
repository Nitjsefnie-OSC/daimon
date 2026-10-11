"""Retain delivered Discord answer chunks without moving the input watermark.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0084_answer_chunk_ids"
down_revision: str | None = "0083_discord_message_admissions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.get_bind().execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.add_column(
        "thread_sessions", sa.Column("delivered_answer_chunk_ids", JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.drop_column("thread_sessions", "delivered_answer_chunk_ids")
