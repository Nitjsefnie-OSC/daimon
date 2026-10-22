"""Map Discord metadata to the single settings privacy predicate; no I/O."""

from daimon.core.config import RecruitmentSettings

import discord


def protected(settings: RecruitmentSettings, channel: object, guild_id: str) -> bool:
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


def protected_for_client(client: object, channel: object, guild_id: str) -> bool:
    """Transport helpers inherit the owning bot's immutable settings.

    Plain SDK clients have no OS runtime and therefore no configured tenants.
    Production helpers receive the bot; REST/MCP readers pass settings directly.
    """
    settings = recruitment_settings_for_client(client)
    resolved_guild = guild_id if guild_id.isdigit() else str(getattr(channel, "guild_id", ""))
    return settings is not None and protected(settings, channel, resolved_guild)


def recruitment_settings_for_client(client: object) -> RecruitmentSettings | None:
    settings = getattr(
        getattr(getattr(client, "runtime", None), "settings", None), "recruitment", None
    )
    return settings if isinstance(settings, RecruitmentSettings) else None
