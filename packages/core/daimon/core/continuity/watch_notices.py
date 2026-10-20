"""Drain migration DMs from the chat adapter's existing notice poller."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from daimon.core.continuity.watch_imports import UNMAPPED_NOTICE
from daimon.core.stores.domain import WatchRow
from daimon.core.stores.watches import claim_import_notice, settle_import_notice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def poll_watch_notices(
    sessions: async_sessionmaker[AsyncSession],
    *,
    deliver: Callable[[WatchRow, str, str], Awaitable[bool]],
) -> None:
    for _ in range(4):
        async with sessions.begin() as db:
            notice = await claim_import_notice(db, now=datetime.now(UTC))
        if notice is None:
            return
        watch, workspace_id = notice
        try:
            delivered = await deliver(watch, workspace_id, UNMAPPED_NOTICE)
        except Exception:
            delivered = False  # Never log owner details or source content.
        async with sessions.begin() as db:
            await settle_import_notice(db, watch, delivered=delivered)
