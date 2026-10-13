"""Join main's GitHub authorized permission with the neutral worker outcome origin.

0094_github_authorized_perm (main) and 0095_turn_outcome_worker_origin (neutral)
both descend from 0087_usage_reconcile_progress. Neither is changed; this merge
only joins the two branches into one head.

downgrade: safe
"""

from __future__ import annotations

revision: str = "0096_merge_main_github_perm"
down_revision: tuple[str, str] = ("0095_turn_outcome_worker_origin", "0094_github_authorized_perm")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
