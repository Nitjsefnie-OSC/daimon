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
    *,
    application_id: int | None = None,
) -> None:
    """Gate slash commands and listener controls before the SDK dispatches them."""
    if not recruitment.tenants:
        dispatch(data)
        return
    own_recruitment_command = (
        data.get("type") == 2
        and application_id is not None
        and str(data.get("application_id")) == str(application_id)
        and data.get("data", {}).get("type") == 1
        and data.get("data", {}).get("name") == "recruitment"
    )
    if own_recruitment_command or str(data.get("data", {}).get("custom_id", "")).startswith(
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
