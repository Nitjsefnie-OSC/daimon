"""Record which Managed Agents sessions a headless send left to be settled.

`usage_reconcile_progress` becomes the per-session settle state: its
`covered_through` watermark stays in use, and the two new columns hold the
pending settle request a headless send writes. The forward/repair cursor
columns (`covered_ids`, `forward_since`, `forward_page`, `repair_lt`,
`repair_since`, `repair_page`, `repair_starved`) are no longer read or
written; they stay so a rollback to the previous release keeps working and
are dropped in a later migration.

It also records when it ran, as the `covered_through` of the reserved row
`backlog-cutoff`: calls made before it are the backlog, which automatic
settles never read (see `daimon.core.usage_sweep`).

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0090_session_settle_request"
down_revision: str | None = "0089_agent_personal_memory_store"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "usage_reconcile_progress", sa.Column("settle_turn_event_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "usage_reconcile_progress",
        sa.Column("settle_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_usage_reconcile_progress_settle_requested_at",
        "usage_reconcile_progress",
        ["settle_requested_at"],
        postgresql_where=sa.text("settle_requested_at IS NOT NULL"),
    )
    op.execute(
        "INSERT INTO usage_reconcile_progress (managed_session_id, covered_through) "
        "VALUES ('backlog-cutoff', now()) ON CONFLICT (managed_session_id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM usage_reconcile_progress WHERE managed_session_id = 'backlog-cutoff'")
    op.drop_index(
        "ix_usage_reconcile_progress_settle_requested_at", table_name="usage_reconcile_progress"
    )
    op.drop_column("usage_reconcile_progress", "settle_requested_at")
    op.drop_column("usage_reconcile_progress", "settle_turn_event_id")
