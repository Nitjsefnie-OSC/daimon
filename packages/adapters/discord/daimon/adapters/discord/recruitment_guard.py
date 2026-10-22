"""Recruitment filter for Discord ingress and every provider-context source."""

from collections.abc import Awaitable, Callable

from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.recruitment_privacy import SourceChannel, privacy_active, sources_for_task

import discord


def source_channel(channel: discord.abc.GuildChannel | discord.Thread) -> SourceChannel:
    return SourceChannel(
        str(channel.id),
        str(channel.guild.id),
        str(channel.parent_id) if isinstance(channel, discord.Thread) else "",
        channel.name,
        isinstance(channel, discord.Thread),
    )


async def recruitment_message_filter(
    runtime: DiscordRuntime, guild_id: str, *, fresh: bool = True
) -> Callable[[discord.Message], Awaitable[bool]]:
    tenant = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    sources = await sources_for_task(runtime.sessionmaker, tenant, fresh=fresh)

    async def blocked(message: discord.Message) -> bool:
        if not sources.active:
            return False
        if (
            not isinstance(message.channel, discord.abc.GuildChannel | discord.Thread)
            or message.guild is None
        ):
            return True

        guild = message.guild

        async def resolve(id: str) -> SourceChannel:
            channel = await guild.fetch_channel(int(id))
            return source_channel(channel)

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

    return blocked


async def recruitment_message_blocked(runtime: DiscordRuntime, message: discord.Message) -> bool:
    if message.guild is None:
        return False
    tenant = derive_tenant_uuid(platform="discord", workspace_id=str(message.guild.id))
    if not await privacy_active(runtime.sessionmaker, tenant):
        return False
    return await (await recruitment_message_filter(runtime, str(message.guild.id)))(message)


async def recruitment_thread_blocked(runtime: DiscordRuntime, thread: discord.Thread) -> bool:
    sources = await sources_for_task(
        runtime.sessionmaker,
        derive_tenant_uuid(platform="discord", workspace_id=str(thread.guild.id)),
        fresh=True,
    )

    async def resolve(id: str) -> SourceChannel:
        return source_channel(await thread.guild.fetch_channel(int(id)))

    return await sources.thread(str(thread.id), resolve)
