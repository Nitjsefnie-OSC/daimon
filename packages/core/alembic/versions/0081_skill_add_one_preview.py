"""Allow one open skill-add preview per person and thread, across agents.

downgrade: safe
"""

from alembic import op

revision: str = "0081_skill_add_one_preview"
down_revision: str | None = "0080_github_grant_proposals"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE pending_skill_adds AS older SET consumed_at = now()
        WHERE older.consumed_at IS NULL AND EXISTS (
            SELECT 1 FROM pending_skill_adds AS newer
            WHERE newer.consumed_at IS NULL
              AND newer.tenant_id = older.tenant_id
              AND newer.account_id = older.account_id
              AND newer.platform = older.platform
              AND newer.thread_id = older.thread_id
              AND (newer.created_at, newer.id) > (older.created_at, older.id)
        )
        """
    )
    op.drop_index("uq_pending_skill_adds_open", table_name="pending_skill_adds")
    op.create_index(
        "uq_pending_skill_adds_open",
        "pending_skill_adds",
        ["tenant_id", "account_id", "platform", "thread_id"],
        unique=True,
        postgresql_where="consumed_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index("uq_pending_skill_adds_open", table_name="pending_skill_adds")
    op.create_index(
        "uq_pending_skill_adds_open",
        "pending_skill_adds",
        ["tenant_id", "account_id", "platform", "thread_id", "ma_agent_id"],
        unique=True,
        postgresql_where="consumed_at IS NULL",
    )
