"""Track GitHub's installation repository snapshot separately from Connect confirmation.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0108_github_installation_truth"
down_revision: str | None = "0107_drop_teams_channel_sites"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "github_app_installations",
        sa.Column("github_repo_full_names", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "github_app_installations",
        sa.Column("github_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "github_app_installations",
        sa.Column("github_removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # A null snapshot allows confirmed repos until the scheduler verifies GitHub.
    op.execute("""
        INSERT INTO github_installation_reconciliations
            (installation_id, generation, state, attempts, available_at)
        SELECT installation_id, 1, 'pending', 0, statement_timestamp()
        FROM github_app_installations WHERE app = 'github_app'
        ON CONFLICT (installation_id) DO UPDATE SET
            generation = github_installation_reconciliations.generation + 1,
            state = CASE WHEN github_installation_reconciliations.state = 'running'
                         THEN 'running' ELSE 'pending' END,
            attempts = 0,
            available_at = CASE WHEN github_installation_reconciliations.state = 'running'
                                THEN github_installation_reconciliations.available_at
                                ELSE statement_timestamp() END,
            last_error = NULL,
            updated_at = statement_timestamp()
    """)


def downgrade() -> None:
    op.drop_column("github_app_installations", "github_removed_at")
    op.drop_column("github_app_installations", "github_refreshed_at")
    op.drop_column("github_app_installations", "github_repo_full_names")
