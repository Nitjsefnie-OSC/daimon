"""Map Discord metadata to the single settings privacy predicate; no I/O."""

from daimon.core.config import RecruitmentSettings

import discord


def protected(settings: RecruitmentSettings, channel: object, guild_id: str) -> bool:
    return settings.is_recruitment_channel(
        guild_id,
        str(getattr(channel, "id", "")),
        str(channel.parent_id)
        if isinstance(channel, discord.Thread) and channel.parent_id
        else None
        if isinstance(channel, discord.Thread)
        else "",
    )
