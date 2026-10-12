"""agent_personal_memory_store — one person's own MA memory store per agent.

One row per (tenant, agent, account): maps the person's account to the
Anthropic memory store (memstore_...) that holds their personal preferences for
that agent. Created lazily on the person's next new session; nothing is
backfilled and the shared `agent_memory_store` is untouched.

`account_id` has no foreign key on purpose: account erasure deletes the account
row before the upstream store, and the binding is what records the store still
to delete. Downgrading drops the bindings; the MA stores themselves remain.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0088_agent_personal_memory_store"
down_revision: str | None = "0087_usage_reconcile_progress"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_personal_memory_store",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("memory_store_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "agent_id", "account_id", name="pk_agent_personal_memory_store"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_agent_personal_memory_store_tenant_id",
        ),
    )
    op.create_index(
        "ix_agent_personal_memory_store_account_id",
        "agent_personal_memory_store",
        ["account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_personal_memory_store_account_id", table_name="agent_personal_memory_store"
    )
    op.drop_table("agent_personal_memory_store")
