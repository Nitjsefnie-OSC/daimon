"""Record what kind of failure stopped a session preparation.

`session_preparations.failure_kind` is null for an ordinary failure and
`skill_unavailable` when the successor could not mount a deleted skill, so a
retry inside the backoff can repeat that refusal. Rows written before the
column existed stay null.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0095_preparation_failure_kind"
down_revision: str | None = "0094_github_authorized_perm"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("session_preparations", sa.Column("failure_kind", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("session_preparations", "failure_kind")
