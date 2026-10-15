"""Per-agent third-party MCP tool rules and append-only edit history.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0104_agent_tool_rules"
down_revision: str | None = "0103_teams_channel_subscriptions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_tool_rules",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("tool_key", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("server_url", sa.Text(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("rule IS NULL OR rule IN ('allow', 'ask', 'deny')"),
        sa.CheckConstraint("scope IN ('chat', 'chat_and_routines')"),
        sa.CheckConstraint("revision > 0"),
        sa.CheckConstraint("tool_key LIKE '%/%'"),
    )
    op.create_index(
        "uq_agent_tool_rules_global",
        "agent_tool_rules",
        ["tenant_id", "agent_id", "tool_key"],
        unique=True,
        postgresql_where=sa.text("channel_id IS NULL"),
    )
    op.create_index(
        "uq_agent_tool_rules_channel",
        "agent_tool_rules",
        ["tenant_id", "agent_id", "tool_key", "channel_id"],
        unique=True,
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )
    op.create_table(
        "agent_tool_rule_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("tool_key", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("before_rule", sa.Text(), nullable=True),
        sa.Column("before_scope", sa.Text(), nullable=True),
        sa.Column("before_agent_name", sa.Text(), nullable=True),
        sa.Column("before_server_url", sa.Text(), nullable=True),
        sa.Column("after_rule", sa.Text(), nullable=True),
        sa.Column("after_scope", sa.Text(), nullable=True),
        sa.Column("after_agent_name", sa.Text(), nullable=False),
        sa.Column("after_server_url", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("agent_tool_rule_history")
    op.drop_table("agent_tool_rules")
