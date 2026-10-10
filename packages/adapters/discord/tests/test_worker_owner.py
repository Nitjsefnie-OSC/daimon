"""A lost owner connection fails stop; recovery waits out its live turns."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from asyncpg import CannotConnectNowError  # pyright: ignore[reportMissingTypeStubs]
from daimon.adapters.discord.runtime import hold_worker_owner
from daimon.core.stores.discord_message_admissions import claim_message
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    list_orphaned_turns,
    lock_orphaned_turn,
    mark_turn_active,
)
from daimon.core.stores.worker_ownership import owner_is_alive
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker


async def test_transient_owner_connection_loss_does_not_cancel_worker(
    db_nullpool_engine: AsyncEngine,
) -> None:
    entered, stopped, ownership_lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owner_key = 718211
    owner_engine = MagicMock(spec=AsyncEngine)
    attempts = 0

    def connect() -> AsyncConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ConnectionRefusedError("database restarting")
        return db_nullpool_engine.connect()

    owner_engine.connect.side_effect = connect

    async def worker() -> None:
        async with hold_worker_owner(
            owner_engine,
            owner_key=owner_key,
            ownership_lost=ownership_lost,
            probe_interval_s=0.01,
        ):
            entered.set()
            await stopped.wait()

    task = asyncio.create_task(worker())
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    owner_pid = text("""
        SELECT pid FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND classid = 0
          AND objid = :key AND objsubid = 1
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
    """)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with factory() as session:
            pid = await session.scalar(owner_pid, {"key": owner_key})
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        await asyncio.wait_for(ownership_lost.wait(), timeout=5)
        assert not task.done(), "the entrypoint must drain the worker after owner loss"
        assert attempts == 1, "a disconnected worker must never reacquire its old key"
        stopped.set()
        await asyncio.wait_for(task, timeout=5)
        async with factory() as session:
            assert await owner_is_alive(session, owner_key), "the fail-stop grace survives exit"
            await session.execute(
                text("""UPDATE discord_worker_heartbeats
                     SET seen_at = clock_timestamp() - interval '121 seconds'
                     WHERE owner_key = :key"""),
                {"key": owner_key},
            )
            assert not await owner_is_alive(session, owner_key)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "reconnect_error", [SQLAlchemyError, ConnectionRefusedError, CannotConnectNowError]
)
async def test_sustained_owner_loss_drains_without_cancellation(
    db_nullpool_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    reconnect_error: type[Exception],
) -> None:
    from daimon.adapters.discord.__main__ import drain_on_owner_loss

    exit_process = MagicMock()
    monkeypatch.setattr("daimon.adapters.discord.__main__.os._exit", exit_process)

    ownership_lost, stopped = asyncio.Event(), asyncio.Event()
    bot = MagicMock()
    bot.runtime.ownership_lost = ownership_lost
    bot._drain_and_close = AsyncMock(side_effect=lambda: stopped.set())
    watcher = asyncio.create_task(drain_on_owner_loss(bot))
    try:
        async with hold_worker_owner(
            db_nullpool_engine,
            owner_key=718212,
            ownership_lost=ownership_lost,
            probe_interval_s=0.01,
        ):
            # The first failed probe immediately signals the fail-stop drain.
            execute = AsyncMock(side_effect=TimeoutError)
            monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncConnection.execute", execute)
            reconnect = MagicMock(side_effect=reconnect_error("offline"))
            monkeypatch.setattr(AsyncEngine, "connect", reconnect)
            await asyncio.wait_for(stopped.wait(), timeout=5)
            await watcher
            assert reconnect.call_count == 0, "a failed owner must not reconnect"
            bot._drain_and_close.assert_awaited_once()
            exit_process.assert_called_once_with(1)
            assert ownership_lost.is_set()
            current = asyncio.current_task()
            assert current is not None and current.cancelling() == 0
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def test_runtime_owner_leaves_the_turn_pool_capacity_available(
    db_nullpool_engine: AsyncEngine,
) -> None:
    from pathlib import Path

    from daimon.adapters.discord.runtime import build_runtime
    from daimon.core.config import Settings

    settings = Settings.model_validate(
        {
            "database": {
                "url": db_nullpool_engine.url.render_as_string(hide_password=False),
                "pool_size": 1,
                "max_overflow": 3,
            },
            "anthropic": {"api_key": "offline-test-key"},
            "crypto": {"allow_plaintext": True},
            "defaults_root": Path("defaults"),
        }
    )
    async with build_runtime(settings) as runtime:
        engine = runtime.sessionmaker.kw["bind"]
        assert engine.pool.checkedout() == 0, "the owner connection must not consume a turn slot"
        async with runtime.sessionmaker() as session:
            assert await owner_is_alive(session, runtime.owner_key)


async def test_connection_loss_cannot_reclaim_live_pending_message(
    db_nullpool_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    owner_key = 718241
    lost = asyncio.Event()
    async with factory() as session:
        tenant = await make_tenant(session)
        await session.commit()
    async with hold_worker_owner(
        db_nullpool_engine, owner_key=owner_key, ownership_lost=lost, probe_interval_s=2
    ):
        async with factory() as session:
            assert await claim_message(
                session, tenant_id=tenant.id, channel_id="1", message_id="2", owner_key=owner_key
            )
            await session.commit()
            pid = await session.scalar(
                text("""SELECT pid FROM pg_locks WHERE locktype = 'advisory'
                     AND granted AND classid = 0 AND objid = :key AND objsubid = 1"""),
                {"key": owner_key},
            )
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        async with factory() as session:
            assert not await claim_message(
                session, tenant_id=tenant.id, channel_id="1", message_id="2", owner_key=718242
            )
            await session.rollback()
        await asyncio.wait_for(lost.wait(), timeout=5)


async def test_connection_loss_cannot_recover_live_card(
    db_nullpool_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    owner_key = 718243
    lost = asyncio.Event()
    async with factory() as session:
        tenant = await make_tenant(session)
        row = await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="owner-connection-loss",
            account_id=uuid4(),
            ma_session_id="owner-connection-loss",
        )
        await mark_turn_active(
            session,
            id=row.id,
            active_turn_message_id="live-card",
            now=datetime.now(UTC),
            owner_key=owner_key,
        )
        await session.commit()
        row = next(
            r for r in await list_orphaned_turns(session, platform="discord") if r.id == row.id
        )
    async with hold_worker_owner(
        db_nullpool_engine, owner_key=owner_key, ownership_lost=lost, probe_interval_s=2
    ):
        async with factory() as session:
            pid = await session.scalar(
                text("""SELECT pid FROM pg_locks WHERE locktype = 'advisory'
                     AND granted AND classid = 0 AND objid = :key AND objsubid = 1"""),
                {"key": owner_key},
            )
            assert pid is not None
            assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await session.commit()
        async with factory() as session:
            assert await lock_orphaned_turn(session, row=row) is None
            await session.rollback()
        await asyncio.wait_for(lost.wait(), timeout=5)
