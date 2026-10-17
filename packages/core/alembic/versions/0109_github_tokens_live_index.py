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

TABLE = "github_issued_tokens"
INDEX = "ix_github_issued_tokens_live_session"


def _table_schema() -> str:
    # Look up and drop the index only in the table's own schema, never a
    # same-named index elsewhere on the search_path.
    schema = op.get_bind().scalar(
        sa.text(
            "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.oid = to_regclass(:t)"
        ),
        {"t": TABLE},
    )
    assert schema is not None, f"{TABLE} not found"
    return schema


def upgrade() -> None:
    with op.get_context().autocommit_block():
        schema = _table_schema()
        invalid = op.get_bind().scalar(
            sa.text(
                "SELECT NOT i.indisvalid FROM pg_index i"
                " JOIN pg_class c ON c.oid = i.indexrelid"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE c.relname = :name AND n.nspname = :schema"
            ),
            {"name": INDEX, "schema": schema},
        )
        if invalid:
            op.drop_index(INDEX, table_name=TABLE, schema=schema, postgresql_concurrently=True)
        op.create_index(
            INDEX,
            TABLE,
            ["session_id"],
            schema=schema,
            postgresql_where=sa.text("status IN ('stored', 'delivered')"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX,
            table_name=TABLE,
            schema=_table_schema(),
            if_exists=True,
            postgresql_concurrently=True,
        )
