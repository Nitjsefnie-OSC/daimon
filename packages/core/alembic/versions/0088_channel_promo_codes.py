"""Promo codes any member redeems into their own channel's budget.

`promo_codes.channel_cap_usd` marks a channel code and caps what one channel
takes from it. `promo_redemptions.channel_id` names the channel a redemption
credited. A server-wide redemption (no channel) stays once per tenant; a
channel redemption is once per person per channel.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

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
    op.add_column("promo_redemptions", sa.Column("channel_id", sa.Text(), nullable=True))
    op.drop_constraint("uq_promo_redemptions_code_tenant", "promo_redemptions", type_="unique")
    op.create_index(
        "uq_promo_redemptions_code_tenant",
        "promo_redemptions",
        ["promo_code_id", "tenant_id"],
        unique=True,
        postgresql_where=sa.text("channel_id IS NULL"),
    )
    op.create_index(
        "uq_promo_redemptions_code_channel_account",
        "promo_redemptions",
        ["promo_code_id", "tenant_id", "channel_id", "redeemed_by_account_id"],
        unique=True,
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Channel codes and their redemptions have no server-wide equivalent. The
    # credit they granted stays in the ledger.
    op.execute(
        "DELETE FROM promo_redemptions WHERE channel_id IS NOT NULL OR promo_code_id IN"
        " (SELECT id FROM promo_codes WHERE channel_cap_usd IS NOT NULL)"
    )
    op.execute("DELETE FROM promo_codes WHERE channel_cap_usd IS NOT NULL")
    op.drop_index("uq_promo_redemptions_code_channel_account", table_name="promo_redemptions")
    op.drop_index("uq_promo_redemptions_code_tenant", table_name="promo_redemptions")
    op.create_unique_constraint(
        "uq_promo_redemptions_code_tenant", "promo_redemptions", ["promo_code_id", "tenant_id"]
    )
    op.drop_column("promo_redemptions", "channel_id")
    op.drop_constraint("ck_promo_codes_channel_cap", "promo_codes", type_="check")
    op.drop_column("promo_codes", "channel_cap_usd")
