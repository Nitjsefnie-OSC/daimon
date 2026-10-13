"""The delivery kind migration backfills what it can tell and round-trips."""

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession

# (delivery_status, delivery_note) before, delivery_kind after.
_ROWS = [
    ("pending", "failure_notice", "failure_notice"),
    ("claimed", "failure_notice", "failure_notice"),
    ("pending", None, "result"),
    ("skipped", "agent_posted", "result"),
    ("delivered", None, None),
    ("skipped", "post_failed", None),
    (None, None, None),
]


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0094_routine_delivery_kind.py"
    spec = importlib.util.spec_from_file_location("migration_routine_delivery_kind", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_backfill_and_round_trip(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    ids = []
    for status, note, _ in _ROWS:
        row = await create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id=None,
            agent_id="agent",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="run",
        )
        await db_session.execute(
            text("UPDATE routines SET delivery_status = :s, delivery_note = :n WHERE id = :id"),
            {"s": status, "n": note, "id": row.id},
        )
        ids.append(row.id)
    migration = _migration()
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def apply(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return apply

    async def kinds() -> list[object]:
        result = await db_session.execute(text("SELECT id, delivery_kind FROM routines"))
        by_id = dict(result.tuples().all())
        return [by_id[i] for i in ids]

    await conn.run_sync(run("downgrade"))
    await conn.run_sync(run("upgrade"))
    assert await kinds() == [kind for *_, kind in _ROWS]

    await db_session.execute(
        text("UPDATE routines SET delivery_note = NULL WHERE delivery_kind = 'failure_notice'")
    )
    await conn.run_sync(run("downgrade"))
    notes = await db_session.execute(text("SELECT id, delivery_note FROM routines"))
    by_id = dict(notes.tuples().all())
    assert [by_id[i] for i in ids[:2]] == ["failure_notice", "failure_notice"], (
        "the previous release still sees a waiting failure notice"
    )
    await conn.run_sync(run("upgrade"))
