"""Settings-only Discord recruitment boundary. No database or title lookups."""

import discord
from daimon.adapters.mcp.runtime import McpRuntime
from fastmcp.exceptions import ToolError


def protected(runtime: McpRuntime, guild_id: str, channel: object) -> bool:
    settings = runtime.settings.recruitment
    if not settings.tenants:
        return False
    parent = getattr(channel, "parent_id", None)
    return settings.is_recruitment_channel(
        guild_id,
        str(getattr(channel, "id", "")),
        str(parent)
        if isinstance(channel, discord.Thread) and parent
        else ""
        if isinstance(channel, discord.abc.GuildChannel)
        else None,
    )


def require_general_channel(runtime: McpRuntime, guild_id: str, channel: object) -> None:
    if protected(runtime, guild_id, channel):
        raise ToolError("Source unavailable.")
