"""Independent connections preserve quotas and revocation under races."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.stores import watches
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import test_callbacks

registered = test_callbacks.registered
event = test_callbacks.event
deliver = test_callbacks.deliver
pytestmark = pytest.mark.fresh_schema


async def test_review_concurrent_cap_is_exact(db_session_factory, registered):
    db_session_factory = async_sessionmaker(
        db_session_factory.kw["bind"].engine, expire_on_commit=False
    )
    async with db_session_factory.begin() as db:
        await db.execute(text("UPDATE watches SET max_fires=1"))
    results = await asyncio.gather(
        deliver(db_session_factory, event(registered)),
        deliver(db_session_factory, event(registered)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, uuid.UUID) for r in results) == 1, results
    async with db_session_factory() as db:
        assert await db.scalar(text("SELECT count(*) FROM watch_inbox")) == 1
        assert await db.scalar(text("SELECT count(*) FROM task_continuations")) == 1


async def test_review_revoke_races_accept(db_session_factory, registered):
    db_session_factory = async_sessionmaker(
        db_session_factory.kw["bind"].engine, expire_on_commit=False
    )

    async def revoke():
        async with db_session_factory.begin() as db:
            await watches.revoke_capabilities(db, registered.id, now=datetime.now(UTC))

    await asyncio.gather(
        deliver(db_session_factory, event(registered)), revoke(), return_exceptions=True
    )
    async with db_session_factory() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM task_continuations WHERE status IN ('pending','claimed')"
                )
            )
            == 0
        )
