"""Refuse reserved-channel controls before SDK reconstruction or modal loading.

Installed before views are registered. Recruitment's own namespace is dispatched
by its cog, which performs recruiter/live-thread authorization independently.
"""

from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from discord.state import ConnectionState
    from discord.types.interactions import Interaction as InteractionPayload

from daimon.adapters.discord.recruitment_guard import protected
from daimon.core.config import RecruitmentSettings

import discord


def dispatch_general_interaction(
    recruitment: RecruitmentSettings,
    state: "ConnectionState",
    data: dict[str, Any],
    dispatch: Callable[[dict[str, Any]], None],
    spawn: Callable[[Coroutine[Any, Any, None]], object],
) -> None:
    """Gate slash commands and listener controls before the SDK dispatches them."""
    if not recruitment.tenants or str(data.get("data", {}).get("custom_id", "")).startswith(
        "recruit:"
    ):
        dispatch(data)
        return
    interaction = discord.Interaction(data=cast("InteractionPayload", data), state=state)
    if protected(recruitment, interaction.channel, str(interaction.guild_id)):

        async def refuse() -> None:
            await interaction.response.send_message("Source unavailable.", ephemeral=True)

        spawn(refuse())
        return
    dispatch(data)
