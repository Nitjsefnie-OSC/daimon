"""Migration 0109 builds and drops the live-token index, and matches the model."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession

INDEX = "ix_github_issued_tokens_live_session"


@pytest.mark.fresh_schema
async def test_live_index_upgrade_downgrade_upgrade_matches_the_model(
    db_session: AsyncSession,
) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0109_github_tokens_live_index.py"
    spec = importlib.util.spec_from_file_location("migration_github_issued_tokens_live", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db_session.connection()

    def present(sync: Connection) -> bool:
        return bool(
            sync.scalar(
                text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
                {"name": INDEX},
            )
        )

    def cycle(sync: Connection) -> None:
        # Concurrent DDL cannot run in a transaction; the migration steps out of
        # the one alembic opens, as `alembic upgrade` does.
        sync.commit()
        context = MigrationContext.configure(sync)
        with context.begin_transaction(), Operations.context(context):
            migration.downgrade()
            assert not present(sync)
            migration.upgrade()
            assert present(sync)
            migration.upgrade()
            assert present(sync)
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [diff for diff in differences if "github_issued_tokens" in str(diff)]

    await connection.run_sync(cycle)
