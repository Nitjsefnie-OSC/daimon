"""MCP serialization uses the same recruitment source policy as provider context."""

import discord
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.recruitment_privacy import ResolveChannel, SourceChannel, sources_for_task


def source_channel(channel: discord.abc.GuildChannel | discord.Thread) -> SourceChannel:
    return SourceChannel(
        str(channel.id),
        str(channel.guild.id),
        str(channel.parent_id) if isinstance(channel, discord.Thread) else "",
        channel.name,
        isinstance(channel, discord.Thread),
    )


async def recruitment_thread(
    runtime: McpRuntime,
    guild_id: str,
    thread_id: str,
    *,
    title: str = "",
    parent_id: str = "",
    resolve: ResolveChannel | None = None,
) -> bool:
    # Indexed/cached names are intentionally not used to establish safety.
    sources = await sources_for_task(
        runtime.session_factory, derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    )

    async def fetch(id: str) -> SourceChannel:
        async with rest_client(_require_bot_token(runtime)) as client:
            channel = await client.fetch_channel(int(id))
            if not isinstance(channel, discord.abc.GuildChannel | discord.Thread):
                raise PermissionError("not_guild_channel")
            return source_channel(channel)

    return await sources.thread(thread_id, resolve or fetch)


async def recruitment_message(runtime: McpRuntime, guild_id: str, message: discord.Message) -> bool:
    sources = await sources_for_task(
        runtime.session_factory, derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    )
    if not sources.active:
        return False
    if (
        not isinstance(message.channel, discord.abc.GuildChannel | discord.Thread)
        or message.guild is None
    ):
        return True

    guild = message.guild

    async def resolve(id: str) -> SourceChannel:
        return source_channel(await guild.fetch_channel(int(id)))

    return await sources.message(
        source_channel(message.channel),
        str(message.id),
        resolve,
        starter_thread_id=str(message.id) if message.flags.has_thread else None,
        notice_thread_id=str(message.reference.channel_id)
        if message.type is discord.MessageType.thread_created
        and message.reference
        and message.reference.channel_id
        else None,
    )


async def begin_recruitment_read(runtime: McpRuntime, guild_id: str) -> None:
    await sources_for_task(
        runtime.session_factory,
        derive_tenant_uuid(platform="discord", workspace_id=guild_id),
        fresh=True,
    )
