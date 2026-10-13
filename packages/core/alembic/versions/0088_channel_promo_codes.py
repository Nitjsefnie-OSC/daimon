"""Promo codes any member redeems into their own channel's budget.

Expand-only, so workers still running the previous release keep working
during the rollout and after an application-only rollback:

- `promo_codes.channel_cap_usd` (nullable) marks a channel code and caps what
  one channel takes from it. Its CHECK holds for every row the old code writes
  (it never sets the column).
- `promo_channel_redemptions` is a new table for channel redemptions, so
  `promo_redemptions` and its `(promo_code_id, tenant_id)` unique constraint,
  which the old `ON CONFLICT` targets, are untouched.
- `promo_redeem_failures.account_id` (nullable) scopes the refusal throttle to
  one person; old workers leave it NULL.

Do not issue channel codes while workers of the previous release still serve
redemptions: they would treat one as a server-wide credit code.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0088_channel_promo_codes"
down_revision: str | None = "0087_usage_reconcile_progress"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("promo_codes", sa.Column("channel_cap_usd", sa.Numeric(12, 2), nullable=True))
    op.create_check_constraint(
        "ck_promo_codes_channel_cap",
        "promo_codes",
        "channel_cap_usd IS NULL OR (kind = 'credit' AND channel_cap_usd >= amount_usd)",
    )
    op.create_table(
        "promo_channel_redemptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "promo_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("promo_codes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column(
            "redeemed_by_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "promo_code_id",
            "tenant_id",
            "channel_id",
            "redeemed_by_account_id",
            name="uq_promo_channel_redemptions_person",
        ),
    )
    op.create_index(
        "promo_channel_redemptions_channel_idx",
        "promo_channel_redemptions",
        ["promo_code_id", "tenant_id", "channel_id"],
    )
    op.create_index(
        "promo_channel_redemptions_tenant_idx", "promo_channel_redemptions", ["tenant_id"]
    )
    op.add_column(
        "promo_redeem_failures",
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "promo_redeem_failures_account_idx",
        "promo_redeem_failures",
        ["tenant_id", "account_id", "attempted_at"],
    )


def downgrade() -> None:
    # Channel codes and their redemptions have no server-wide equivalent. The
    # credit they granted stays in the ledger.
    op.drop_index("promo_redeem_failures_account_idx", table_name="promo_redeem_failures")
    op.drop_column("promo_redeem_failures", "account_id")
    op.drop_table("promo_channel_redemptions")
    op.execute("DELETE FROM promo_codes WHERE channel_cap_usd IS NOT NULL")
    op.drop_constraint("ck_promo_codes_channel_cap", "promo_codes", type_="check")
    op.drop_column("promo_codes", "channel_cap_usd")
