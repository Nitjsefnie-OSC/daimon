"""Migration 0108 queues GitHub truth refreshes without replacing Connect confirmation."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.fresh_schema
async def test_existing_github_app_rows_are_queued_for_first_snapshot(
    db_session: AsyncSession,
) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0108_github_installation_truth.py"
    spec = importlib.util.spec_from_file_location("migration_github_installation_truth", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def inner(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    await conn.run_sync(run("downgrade"))
    await db_session.execute(
        text("""
        INSERT INTO github_app_installations
            (installation_id, account_login, repo_full_names, app)
        VALUES (9016, 'owner', ARRAY['owner/confirmed'], 'github_app'),
               (9017, 'legacy', ARRAY['legacy/repo'], 'legacy')
    """)
    )
    await conn.run_sync(run("upgrade"))
    rows = (
        await db_session.execute(
            text("""
        SELECT i.installation_id, i.repo_full_names, i.github_repo_full_names,
               i.github_refreshed_at, i.github_removed_at, q.state
        FROM github_app_installations i
        LEFT JOIN github_installation_reconciliations q USING (installation_id)
        ORDER BY i.installation_id
    """)
        )
    ).all()
    assert rows[0] == (9016, ["owner/confirmed"], None, None, None, "pending")
    assert rows[1] == (9017, ["legacy/repo"], None, None, None, None)
