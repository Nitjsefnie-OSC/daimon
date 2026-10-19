"""Track private routine allocation and stop obligations durably.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0114_routine_sessions"
down_revision: str | None = "0113_unattended_turn_provenance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "routine_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("routine_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fire_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ma_agent_id", sa.Text(), nullable=False),
        sa.Column("ma_session_id", sa.Text()),
        sa.Column(
            "discovered_session_ids", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("mapping_id", postgresql.UUID(as_uuid=True)),
        sa.Column("owner_key", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="recorded"),
        sa.Column("stop_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("failure_detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("notice_claimed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('recorded', 'session_created', 'running', 'stopping', "
            "'completed', 'stopped', 'stop_unresolved')",
            name="ck_routine_sessions_state",
        ),
        sa.CheckConstraint("stop_attempts BETWEEN 0 AND 3", name="ck_routine_sessions_attempts"),
    )
    op.create_index(
        "routine_sessions_pending_idx", "routine_sessions", ["state", "next_attempt_at"]
    )
    op.create_index("routine_sessions_routine_idx", "routine_sessions", ["tenant_id", "routine_id"])


def downgrade() -> None:
    op.drop_table("routine_sessions")
