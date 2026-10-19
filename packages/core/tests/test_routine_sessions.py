"""Independent routine finalization and provider-confirmed cessation."""

import asyncio
import uuid
from functools import partial

import httpx
import pytest
from daimon.core._models import RoutineSession
from daimon.core.mux_backend import resource_scope
from daimon.core.routine_sessions import RoutineSessionSupervisor
from daimon.core.session_ports_compat import create_session_record
from daimon.core.stores import routine_sessions as store
from daimon.testing.ma import FakeMAState, build_fake_anthropic
from daimon.testing.ma_models import ma_agent
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
def db_session_factory(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def test_intent_write_failure_refuses_provider_creation(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    calls = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("no allocation without durable intent")

    client = build_fake_anthropic(transport)
    supervisor = RoutineSessionSupervisor(db_session_factory, client, owner_key=1 << 100)
    try:
        with supervisor.fire(uuid.uuid4()) as fire:
            metadata = {}
            scope = resource_scope(
                tenant_id=str(uuid.uuid4()), account_id=str(uuid.uuid4()), authorization_id="test"
            )
            with pytest.raises(DBAPIError):
                await fire.allocate(
                    partial(
                        create_session_record,
                        client,
                        agent="ag_test",
                        environment_id="env_test",
                        scope=scope,
                        metadata=metadata,
                        resources=None,
                    ),
                    tenant_id=uuid.UUID(scope.tenant_id),
                    account_id=uuid.UUID(scope.account_id),
                    agent_id="ag_test",
                    metadata=metadata,
                )
        await supervisor.drain()
        assert calls == []
    finally:
        await supervisor.abort_pending()
        await client.close()


async def test_cancelled_completion_wait_cannot_lose_a_confirmed_success(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    sm = db_session_factory
    tenant, account = uuid.uuid4(), uuid.uuid4()
    state = FakeSessionsState(FakeMAState())
    agent = ma_agent(id="ag_test", name="test")
    state.ma.agents[agent.id] = agent.model_dump(mode="json")
    client = build_fake_anthropic(make_fake_sessions_handler(state))
    supervisor = RoutineSessionSupervisor(sm, client, owner_key=123)
    scope = resource_scope(tenant_id=str(tenant), account_id=str(account), authorization_id="test")
    try:
        async with sm() as lock:
            with supervisor.fire(uuid.uuid4()) as fire:
                metadata = {"daimon_tenant": str(tenant), "daimon_account": str(account)}
                created = await fire.allocate(
                    partial(
                        create_session_record,
                        client,
                        agent=agent.id,
                        environment_id="env_test",
                        scope=scope,
                        metadata=metadata,
                        resources=None,
                    ),
                    tenant_id=tenant,
                    account_id=account,
                    agent_id=agent.id,
                    metadata=metadata,
                )
                await fire.running(created.id)
                await lock.execute(
                    select(RoutineSession)
                    .where(RoutineSession.id == fire.intents[0])
                    .with_for_update()
                )
                waiter = asyncio.create_task(fire.completed(created.id))
                # Let completed register its independently owned transaction.
                await asyncio.sleep(0)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert supervisor.completions
            # Fire exit submitted a stop, but it must first join completion.
            await lock.rollback()
        await supervisor.drain()
        async with sm() as db:
            row = await store.get(db, fire.intents[0])
        assert row is not None and row.state == "completed"
        assert row.stop_attempts == 0
        assert not supervisor._tasks
    finally:
        await supervisor.abort_pending()
        await client.close()
