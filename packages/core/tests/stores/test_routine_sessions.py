"""Durable routine outcomes cannot be erased by deletion or late callbacks."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core._models import RoutineSession
from daimon.core.stores import routine_sessions as store
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
def db_session_factory(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def test_terminal_outcomes_are_immutable(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    sm = db_session_factory
    async with sm.begin() as db:
        row = await store.record(
            db,
            routine_id=uuid.uuid4(),
            fire_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            ma_agent_id="ag_private",
            owner_key=123,
        )
    async with sm.begin() as db:
        await store.transition(db, row.id, "session_created", session_id="sess_private")
        await store.transition(db, row.id, "running")
        await store.transition(db, row.id, "completed")
    async with sm.begin() as db:
        after = await store.transition(db, row.id, "stopping", failure="late cancel")
        assert after.state == "completed"
        assert after.failure_detail is None
        assert after.finished_at is not None


async def test_attempts_survive_recovery_and_exhaust(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    sm = db_session_factory
    async with sm.begin() as db:
        row = await store.record(
            db,
            routine_id=uuid.uuid4(),
            fire_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            ma_agent_id="ag_private",
            owner_key=123,
        )
    for attempt in range(1, 4):
        async with sm.begin() as db:
            claimed = await store.begin_attempt(db, row.id, owner_key=123)
            assert claimed is not None and claimed.stop_attempts == attempt
            assert claimed.state == "stopping"
        async with sm.begin() as db:
            assert await store.begin_attempt(db, row.id, owner_key=123) is None
            await db.execute(
                update(RoutineSession)
                .where(RoutineSession.id == row.id)
                .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    async with sm.begin() as db:
        exhausted = await store.begin_attempt(db, row.id, owner_key=123)
        assert exhausted is not None and exhausted.state == "stop_unresolved"
        assert exhausted.id == row.id and exhausted.ma_session_id is None
        status = await store.status(db, routine_id=row.routine_id, tenant_id=row.tenant_id)
        assert status.stop_unresolved == 1 and status.pending == 0


async def test_deleting_a_routine_cannot_delete_its_pending_stop(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    from daimon.core.stores.routines import create_routine, delete_routine
    from daimon.testing.factories import make_tenant

    sm = db_session_factory
    async with sm.begin() as db:
        tenant = await make_tenant(db)
        routine = await create_routine(
            db,
            tenant_id=tenant.id,
            created_by_user_id="owner",
            agent_id="ag_test",
            agent_name="test",
            cron_expr="* * * * *",
            timezone_="UTC",
            trigger_message="work",
        )
        intent = await store.record(
            db,
            routine_id=routine.id,
            fire_id=uuid.uuid4(),
            tenant_id=tenant.id,
            account_id=uuid.uuid4(),
            ma_agent_id="ag_test",
            owner_key=123,
        )
        await store.transition(db, intent.id, "stopping", failure="x" * 1000)
    async with sm.begin() as db:
        assert await delete_routine(db, routine.id, tenant_id=tenant.id)
    async with sm() as db:
        pending = await store.get(db, intent.id)
        assert pending is not None and pending.state == "stopping"
        assert pending.failure_detail == "x" * 500
        assert any(row.id == intent.id for row in await store.pending(db))


async def test_stale_identity_map_cannot_overwrite_another_transactions_terminal_outcome(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    sm = db_session_factory
    async with sm.begin() as db:
        row = await store.record(
            db,
            routine_id=uuid.uuid4(),
            fire_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            ma_agent_id="ag_private",
            owner_key=123,
        )
        await store.transition(db, row.id, "session_created", session_id="session_private")
        await store.transition(db, row.id, "running")
    async with sm() as stale:
        held = await stale.get(RoutineSession, row.id)
        assert held is not None and held.state == "running"
        await stale.commit()
        async with sm.begin() as current:
            completed = await store.transition(current, row.id, "completed")
        async with stale.begin():
            result = await store.transition(stale, row.id, "stopping", failure="late cancel")
        assert result.state == "completed"
        assert result.finished_at == completed.finished_at
        assert result.failure_detail is None
