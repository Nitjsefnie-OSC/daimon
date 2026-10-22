"""Withhold recruitment sources from all agent-facing Discord reads."""

import discord
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.recruitment import is_recruitment_source


async def recruitment_thread(
    runtime: McpRuntime, guild_id: str, thread_id: str, *, title: str = "", parent_id: str = ""
) -> bool:
    return await is_recruitment_source(
        runtime.session_factory,
        derive_tenant_uuid(platform="discord", workspace_id=guild_id),
        thread_id,
        title=title,
        parent_id=parent_id,
    )


async def recruitment_message(runtime: McpRuntime, guild_id: str, message: discord.Message) -> bool:
    channel = message.channel
    if isinstance(channel, discord.Thread) and await recruitment_thread(
        runtime, guild_id, str(channel.id), title=channel.name, parent_id=str(channel.parent_id)
    ):
        return True
    if await recruitment_thread(runtime, guild_id, str(message.id)):
        return True
    thread = message.thread
    if thread is None and message.flags.has_thread and message.guild:
        try:
            fetched = await message.guild.fetch_channel(message.id)
            thread = fetched if isinstance(fetched, discord.Thread) else None
        except (discord.NotFound, discord.Forbidden):
            return True
    if thread and await recruitment_thread(
        runtime, guild_id, str(thread.id), title=thread.name, parent_id=str(thread.parent_id)
    ):
        return True
    if (
        message.type is discord.MessageType.thread_created
        and message.reference
        and message.reference.channel_id
    ):
        return await recruitment_thread(
            runtime,
            guild_id,
            str(message.reference.channel_id),
            title=message.content,
            parent_id=str(channel.id),
        )
    return False
