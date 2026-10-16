"""Persist Gemini records and conservative holds on the application state DB.

downgrade: unsupported
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0106_gemini_runtime_state"
down_revision: str | None = "0105_github_app_only"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "gemini_runtime_state",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "account_id", "project", "kind"),
        sa.CheckConstraint("kind IN ('records', 'holds')", name="ck_gemini_runtime_state_kind"),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Gemini runtime state retains native records and unresolved financial holds"
    )
