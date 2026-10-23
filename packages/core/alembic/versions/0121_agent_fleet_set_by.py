"""agent_fleet_set_by: who wrote an agent's fleet grant or its "off" marker.

`agent_backend_revision.fleet_set_by` is `admin` or `owner` on a revision whose
fleet setting an admin or the agent's owner wrote, and is carried forward by every
other change. A non-admin owner may never loosen an admin's decision: an admin's
"off" stays off for them, and an admin's grant is their ceiling instead of the
default. Null on existing rows; a stored grant or off with no writer reads as an
admin's (fail closed). Additive only; downgrading drops the column, and owners
then fall back to the default-or-current ceiling.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0121_agent_fleet_set_by"
down_revision: str | None = "0120_mcp_token_surface"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("agent_backend_revision", sa.Column("fleet_set_by", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_agent_backend_fleet_set_by",
        "agent_backend_revision",
        "fleet_set_by IS NULL OR fleet_set_by IN ('admin', 'owner')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_agent_backend_fleet_set_by", "agent_backend_revision", type_="check")
    op.drop_column("agent_backend_revision", "fleet_set_by")
