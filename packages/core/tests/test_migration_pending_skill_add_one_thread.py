"""The skill-add preview index migrates between agent and thread scope."""

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_pending_skill_add_one_thread_migration_round_trip(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0081_skill_add_one_preview.py"
    spec = importlib.util.spec_from_file_location("migration_pending_skill_add_one_thread", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def inner(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    async def index_columns() -> list[str]:
        result = await db_session.execute(
            text(
                "SELECT a.attname FROM pg_class t "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "JOIN pg_index i ON i.indrelid = t.oid "
                "JOIN pg_class x ON x.oid = i.indexrelid "
                "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, pos) ON true "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                "WHERE n.nspname = current_schema() AND t.relname = 'pending_skill_adds' "
                "AND x.relname = 'uq_pending_skill_adds_open' ORDER BY k.pos"
            )
        )
        return list(result.scalars())

    expected = ["tenant_id", "account_id", "platform", "thread_id"]
    assert await index_columns() == expected
    await conn.run_sync(run("downgrade"))
    assert await index_columns() == [*expected, "ma_agent_id"]
    await conn.run_sync(run("upgrade"))
    assert await index_columns() == expected


def test_every_revision_id_fits_alembic_version_column() -> None:
    """alembic_version.version_num is varchar(32); a longer id fails every upgrade."""
    versions = Path(__file__).parents[1] / "alembic/versions"
    too_long = []
    for path in versions.glob("*.py"):
        for line in path.read_text().splitlines():
            if line.startswith("revision"):
                revision = line.split("=", 1)[1].strip().strip("\"'")
                if len(revision) > 32:
                    too_long.append(revision)
    assert too_long == []
