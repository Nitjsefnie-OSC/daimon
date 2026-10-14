"""Keep the external admission finding on live GitHub sessions.

downgrade: drops the stored finding
"""

from alembic import op

revision: str = "0098_external_github_session"
down_revision: str | None = "0097_one_github_token"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE thread_sessions ADD COLUMN is_external boolean NOT NULL DEFAULT false")
    op.execute(
        "ALTER TABLE github_app_session_vaults "
        "ADD COLUMN is_external boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "UPDATE thread_sessions AS session SET is_external = true "
        "FROM accounts AS account WHERE session.account_id = account.id "
        "AND account.is_external"
    )
    op.execute(
        "UPDATE thread_sessions AS session SET is_external = true "
        "WHERE EXISTS (SELECT 1 FROM turn_origins AS origin "
        "WHERE origin.tenant_id = session.tenant_id "
        "AND origin.account_id = session.account_id "
        "AND origin.platform = session.platform "
        "AND origin.thread_id = session.thread_id AND origin.is_external)"
    )
    op.execute(
        "UPDATE github_app_session_vaults AS vault SET is_external = true "
        "FROM accounts AS account WHERE vault.account_id = account.id "
        "AND account.is_external"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE github_app_session_vaults DROP COLUMN is_external")
    op.execute("ALTER TABLE thread_sessions DROP COLUMN is_external")
