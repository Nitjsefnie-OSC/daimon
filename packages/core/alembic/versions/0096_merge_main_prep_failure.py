"""Join main's preparation failure kind with the neutral worker outcome origin.

0095_preparation_failure_kind (main) and 0095_turn_outcome_worker_origin
(neutral) both descend from 0087_usage_reconcile_progress. Neither is
changed; this merge only joins the two branches into one head.

downgrade: safe
"""

from __future__ import annotations

revision: str = "0096_merge_main_prep_failure"
down_revision: tuple[str, str] = (
    "0095_turn_outcome_worker_origin",
    "0095_preparation_failure_kind",
)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
