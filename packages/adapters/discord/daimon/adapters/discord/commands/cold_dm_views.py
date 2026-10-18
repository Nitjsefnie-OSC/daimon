"""Requester-bound controls for private server selection."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress

import structlog

import discord

SelectServer = Callable[[discord.Interaction, str], Awaitable[None]]
SwitchServer = Callable[[discord.Interaction], Awaitable[None]]


class _PrivateControl(discord.ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        structlog.get_logger(__name__).warning(
            "discord.dm.control_failed",
            user_id=str(interaction.user.id),
            route_key=str(interaction.channel_id),
            reason="control_unavailable",
            error_type=type(error).__name__,
        )


class ServerSelector(_PrivateControl):
    def __init__(
        self,
        *,
        user_id: int,
        route_id: int,
        servers: Sequence[tuple[str, str]],
        choose: SelectServer,
        expire: Callable[[], None],
    ) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.route_id = route_id
        self.servers = list(servers)
        self.choose = choose
        self.expire = expire
        # Discord refreshes View.timeout on interaction; retained text has an
        # absolute lifetime even when someone pages through the selector.
        self._expiry = asyncio.get_running_loop().call_later(300, self._expire)
        self.message: discord.Message | None = None
        self.page = 0
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id or interaction.channel_id != self.route_id:
            await interaction.response.send_message(
                "This selector belongs to another conversation.", ephemeral=True
            )
            return False
        return True

    def complete(self) -> None:
        """Retire an accepted selector immediately, before admission or model execution."""
        self._expiry.cancel()
        self.stop()

    def _expire(self) -> None:
        self.expire()
        self.stop()
        asyncio.create_task(self.on_timeout())

    async def on_timeout(self) -> None:
        self._expiry.cancel()
        self.expire()
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(
                    content="This selector expired. Send a new message.", view=None
                )

    def _render(self) -> None:
        self.clear_items()
        selector = discord.ui.Select[ServerSelector](
            placeholder="Choose a server",
            options=[
                discord.SelectOption(label=name[:100], value=tenant)
                for tenant, name in self.servers[self.page * 25 : (self.page + 1) * 25]
            ],
        )

        async def selected(interaction: discord.Interaction) -> None:
            await self.choose(interaction, selector.values[0])

        selector.callback = selected
        self.add_item(selector)
        if len(self.servers) > 25:
            previous = discord.ui.Button[ServerSelector](label="Previous", disabled=self.page == 0)
            following = discord.ui.Button[ServerSelector](
                label="Next", disabled=(self.page + 1) * 25 >= len(self.servers)
            )

            async def back(interaction: discord.Interaction) -> None:
                self.page -= 1
                self._render()
                await interaction.response.edit_message(view=self)

            async def forward(interaction: discord.Interaction) -> None:
                self.page += 1
                self._render()
                await interaction.response.edit_message(view=self)

            previous.callback = back
            following.callback = forward
            self.add_item(previous)
            self.add_item(following)


class ServerSwitch(_PrivateControl):
    def __init__(self, *, user_id: int, route_id: int, switch: SwitchServer) -> None:
        super().__init__(timeout=None)
        self.user_id = user_id
        self.route_id = route_id
        button = discord.ui.Button[ServerSwitch](
            label="Switch server", style=discord.ButtonStyle.secondary
        )

        async def clicked(interaction: discord.Interaction) -> None:
            await switch(interaction)

        button.callback = clicked
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id or interaction.channel_id != self.route_id:
            await interaction.response.send_message(
                "This control belongs to another conversation.", ephemeral=True
            )
            return False
        return True
