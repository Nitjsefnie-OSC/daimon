"""Drop `teams_channel_sites`: channel files no longer need a per-site grant.

Under `Files.ReadWrite.All` every channel's Files folder, private and shared
ones included, is found through Graph's `filesFolder`, so the folders an
admin's Enable files sign-in stored are not read any more. The downgrade
recreates the table empty.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0107_drop_teams_channel_sites"
down_revision: str | None = "0106_gemini_runtime_state"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("teams_channel_sites")


def downgrade() -> None:
    op.create_table(
        "teams_channel_sites",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Text(), nullable=False),
        sa.Column("drive_id", sa.Text(), nullable=False),
        sa.Column("folder_id", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_teams_channel_sites"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_teams_channel_sites_tenants"
        ),
    )
