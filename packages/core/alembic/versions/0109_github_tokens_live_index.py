"""Index the live GitHub issued tokens.

Every status, session and expiry read on `github_issued_tokens` filters to
`stored` or `delivered` rows, but the table had only its primary key, so each
read scanned every row, revoked ones included. A partial index over the live
rows serves them all and stays small. The index is built and dropped
concurrently, outside the migration transaction, so token minting keeps
writing meanwhile. A run cut off mid-build leaves an invalid index behind; a
retried run drops that and builds again, and keeps an index that is already
valid.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0109_github_tokens_live_index"
down_revision: str | None = "0108_github_installation_truth"
branch_labels: str | None = None
depends_on: str | None = None

INDEX = "ix_github_issued_tokens_live_session"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": INDEX},
        )
        if invalid:
            op.drop_index(INDEX, table_name="github_issued_tokens", postgresql_concurrently=True)
        op.create_index(
            INDEX,
            "github_issued_tokens",
            ["session_id"],
            postgresql_where=sa.text("status IN ('stored', 'delivered')"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX,
            table_name="github_issued_tokens",
            if_exists=True,
            postgresql_concurrently=True,
        )
