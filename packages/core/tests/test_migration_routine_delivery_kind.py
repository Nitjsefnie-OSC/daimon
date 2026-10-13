"""The delivery kind migration backfills, round-trips, and survives the previous release's writes."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.routine_delivery import render_fallback_post
from daimon.core.stores.routines import create_routine, get_routine, record_result
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

# The previous release's outbox writes, which know nothing of `delivery_kind`:
# its `queue_failure_notice` and its `record_result(delivery="pending")`.
_OLD_FAILURE = (
    "UPDATE routines SET delivery_status = 'pending', delivery_note = 'failure_notice', "
    "delivery_payload = 'The request to the AI model failed.' WHERE id = :id"
)
_OLD_RESULT = (
    "UPDATE routines SET delivery_status = 'pending', delivery_note = NULL, "
    "delivery_payload = 'All green.' WHERE id = :id"
)


async def _migrate(session: AsyncSession, step: str) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0094_routine_delivery_kind.py"
    spec = importlib.util.spec_from_file_location("migration_routine_delivery_kind", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def apply(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, step)()

    await (await session.connection()).run_sync(apply)


async def _routine(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    row = await create_routine(
        session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        agent_id="agent",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="run",
        destination_kind="channel",
        destination_id="111",
    )
    return row.id


async def _column(session: AsyncSession, column: str, routine_id: uuid.UUID) -> object:
    result = await session.execute(
        text(f"SELECT {column} FROM routines WHERE id = :id"), {"id": routine_id}
    )
    return result.scalar_one()


@pytest.mark.fresh_schema
async def test_backfill_and_round_trip(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    ids = []
    for status, note, _ in _ROWS:
        routine_id = await _routine(db_session, tenant.id)
        await db_session.execute(
            text("UPDATE routines SET delivery_status = :s, delivery_note = :n WHERE id = :id"),
            {"s": status, "n": note, "id": routine_id},
        )
        ids.append(routine_id)

    await _migrate(db_session, "downgrade")
    await _migrate(db_session, "upgrade")

    assert [await _column(db_session, "delivery_kind", i) for i in ids] == [
        kind for *_, kind in _ROWS
    ]
    await _migrate(db_session, "downgrade")
    await _migrate(db_session, "upgrade")


@pytest.mark.fresh_schema
async def test_a_result_the_previous_release_writes_after_the_backfill_stays_a_result(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    routine_id = await _routine(db_session, tenant.id)
    await _migrate(db_session, "downgrade")
    await db_session.execute(text(_OLD_FAILURE), {"id": routine_id})
    await _migrate(db_session, "upgrade")
    assert await _column(db_session, "delivery_kind", routine_id) == "failure_notice"

    await db_session.execute(text(_OLD_RESULT), {"id": routine_id})

    row = await get_routine(db_session, routine_id, tenant_id=tenant.id)
    assert row is not None
    assert (
        render_fallback_post(row)
        == "Result from daimon's daily routine at 09:00 UTC:\n\nAll green."
    )
    await _migrate(db_session, "downgrade")
    assert await _column(db_session, "delivery_note", routine_id) is None, "still a result"
    await _migrate(db_session, "upgrade")


@pytest.mark.fresh_schema
async def test_a_failure_the_previous_release_writes_over_a_result_reads_as_a_failure(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    routine_id = await _routine(db_session, tenant.id)
    await record_result(db_session, routine_id, tail="All green.", error=None, delivery="pending")
    await db_session.execute(
        text("UPDATE routines SET delivery_status = 'delivered' WHERE id = :id"),
        {"id": routine_id},
    )

    await db_session.execute(text(_OLD_FAILURE), {"id": routine_id})

    row = await get_routine(db_session, routine_id, tenant_id=tenant.id)
    assert row is not None and row.delivery_kind == "result", "the old write leaves the kind"
    assert render_fallback_post(row).startswith(
        "daimon's daily routine at 09:00 UTC failed, so there's no result this time."
    )
