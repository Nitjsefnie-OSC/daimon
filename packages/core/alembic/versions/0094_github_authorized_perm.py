"""Record the GitHub permission of the person who authorized a repo.

`tenant_github_repos.authorized_permission` holds the confirming person's
permission on the repo when they authorized it: `admin`, `maintain`, `write`,
`triage` or `read`. Rows authorized before the column existed stay null.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0094_github_authorized_perm"
down_revision: str | None = "0093_spend_gate_indexes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tenant_github_repos",
        sa.Column("authorized_permission", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_github_repos", "authorized_permission")
