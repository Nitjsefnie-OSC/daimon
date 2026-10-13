"""Allow a settled worker batch to wake its orchestrator.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0094_worker_finished_wakes"
down_revision: str | None = "0093_fleet_runs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied', 'timer', "
        "'github_access_ready', 'worker_finished')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # A previous application cannot interpret these rows; never relabel them
    # as a handoff or timer and accidentally run their work through that path.
    op.execute("DELETE FROM task_continuations WHERE reason = 'worker_finished'")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied', 'timer', 'github_access_ready')",
    )
