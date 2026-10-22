"""Live owner access checks shared by watch admission and output delivery."""

from __future__ import annotations

from copy import copy

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
        current = await thread.guild.fetch_channel(thread.id)
        if (
            not isinstance(current, discord.Thread)
            or str(current.id) != row.thread_id
            or str(current.parent_id) != row.parent_channel_id
        ):
            raise ValueError("Reporting thread binding changed")
        thread = current
        member = await thread.guild.fetch_member(int(row.requester_external_user_id))
        roles = await thread.guild.fetch_roles()
        # Use isolated REST snapshots: permissions_for and Member.guild_permissions
        # otherwise resolve role IDs through the gateway's potentially stale cache.
        guild = copy(thread.guild)
        guild._roles = {role.id: role for role in roles}  # pyright: ignore[reportPrivateUsage]
        member = copy(member)
        member.guild = guild
        parent = await thread.guild.fetch_channel(thread.parent_id)
        if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            reason = "Watch paused: reporting thread parent is unavailable."
        else:
            parent = copy(parent)
            parent.guild = guild
            permissions = parent.permissions_for(member)
            if not permissions.view_channel:
                reason = "Watch paused: owner can no longer view the reporting channel."
            elif not permissions.read_message_history:
                reason = "Watch paused: owner can no longer read reporting channel history."
            elif not permissions.send_messages_in_threads:
                reason = "Watch paused: owner can no longer post in the reporting thread."
            elif thread.locked and not permissions.manage_threads:
                reason = "Watch paused: reporting thread is locked and owner cannot manage threads."
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
