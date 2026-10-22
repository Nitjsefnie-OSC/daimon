"""Live owner access checks shared by watch admission and output delivery."""

from __future__ import annotations

from aiohttp import ClientError
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.watches import get_firing, pause_watch
from daimon.core.turn.errors import AdmissionDenied
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord


async def require_watch_thread_access(
    sessions: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    thread: discord.Thread,
) -> discord.Member:
    """Never use cached thread membership or redirect a refused wake to its parent."""
    reason: str | None = None
    member: discord.Member | None = None
    try:
        member = await thread.guild.fetch_member(int(row.requester_external_user_id))
        parent = await thread.guild.fetch_channel(thread.parent_id)
        if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            reason = "Watch paused: reporting thread parent is unavailable."
        else:
            permissions = parent.permissions_for(member)
            if not permissions.view_channel:
                reason = "Watch paused: owner can no longer view the reporting channel."
            elif not permissions.read_message_history:
                reason = "Watch paused: owner can no longer read reporting channel history."
            elif not permissions.send_messages_in_threads:
                reason = "Watch paused: owner can no longer post in the reporting thread."
            elif (
                thread.type is discord.ChannelType.private_thread and not permissions.manage_threads
            ):
                # REST, not Thread.get_member: gateway membership may be stale.
                await thread.fetch_member(member.id)
    except discord.NotFound:
        reason = "Watch paused: reporting thread or owner membership is unavailable."
    except (discord.HTTPException, discord.ClientException, ClientError, OSError, ValueError):
        reason = "Watch paused: owner access to the reporting thread could not be verified."
    if reason is not None:
        async with sessions.begin() as db:
            firing = await get_firing(db, row.idempotency_key)
            if firing is not None:
                await pause_watch(db, firing.watch_id, reason=reason)
        raise AdmissionDenied(reason="invoker_not_allowed")
    assert member is not None
    return member
