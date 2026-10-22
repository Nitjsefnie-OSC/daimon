"""Prevent staffing input from starting ordinary or shared provider turns."""

from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.recruitment import is_recruitment_source

import discord


async def recruitment_message_blocked(runtime: DiscordRuntime, message: discord.Message) -> bool:
    if message.guild is None:
        return False
    tenant = derive_tenant_uuid(platform="discord", workspace_id=str(message.guild.id))
    channel = message.channel
    if isinstance(channel, discord.Thread) and await is_recruitment_source(
        runtime.sessionmaker,
        tenant,
        str(channel.id),
        title=channel.name,
        parent_id=str(channel.parent_id),
    ):
        return True
    if await is_recruitment_source(runtime.sessionmaker, tenant, str(message.id)):
        return True
    thread = message.thread
    if thread is None and message.flags.has_thread:
        try:
            fetched = await message.guild.fetch_channel(message.id)
            thread = fetched if isinstance(fetched, discord.Thread) else None
        except (discord.NotFound, discord.Forbidden):
            return True
    return bool(
        thread
        and await is_recruitment_source(
            runtime.sessionmaker,
            tenant,
            str(thread.id),
            title=thread.name,
            parent_id=str(thread.parent_id),
        )
    )
