"""Say what a routine's outbox post is in `routines.delivery_kind`.

`delivery_note` said both why a post was skipped and, as `failure_notice`,
that the pending post is a failed run's notice rather than a result. The kind
moves to its own nullable column: `result` or `failure_notice`. Existing rows
are backfilled where the kind can be told: a `failure_notice` note, a pending
or claimed result, and a result skipped because the agent posted it itself.
Expand-only: `delivery_note` stays and keeps its values, so the previous
release still reads the rows it wrote.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0094_routine_delivery_kind"
down_revision: str | None = "0093_spend_gate_indexes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("routines", sa.Column("delivery_kind", sa.Text(), nullable=True))
    op.execute(
        "UPDATE routines SET delivery_kind = CASE WHEN delivery_note = 'failure_notice' "
        "THEN 'failure_notice' ELSE 'result' END "
        "WHERE delivery_note IN ('failure_notice', 'agent_posted') "
        "OR (delivery_status IN ('pending', 'claimed') AND delivery_note IS NULL)"
    )


def downgrade() -> None:
    # The previous release reads a waiting failure notice from the note.
    op.execute(
        "UPDATE routines SET delivery_note = 'failure_notice' "
        "WHERE delivery_kind = 'failure_notice' AND delivery_status IN ('pending', 'claimed')"
    )
    op.drop_column("routines", "delivery_kind")
