"""Merge Discord admissions and session budget channel migrations.

Revision ID: 02afddaa7eab
Revises: 0082_discord_message_admissions, 0082_session_budget_channel
downgrade: unsupported
"""

revision: str = "02afddaa7eab"
down_revision: tuple[str, str] = ("0082_discord_message_admissions", "0082_session_budget_channel")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Downgrading across the merged migration branches is unsupported")
