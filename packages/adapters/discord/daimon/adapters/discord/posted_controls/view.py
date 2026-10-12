"""Discord renderer for a `PostedCard`, on the editing side.

The Discord bot process (worker VM) re-renders a posted card in place once
the form comes back; the MCP process (Cloud Run) posted it in the first
place. The two cannot import each other (import-linter's independence
contract), and `daimon.core.posted_controls` deliberately carries no Discord
SDK type, so each side owns a copy of this translation —
`daimon.adapters.mcp.tools.discord._posted_card` is the other one. The two
bodies preserve the same serialized components by default, and
`tests/parity/test_posted_card_renderer_parity.py` is the executable guard:
a divergent default here would silently desync what a user sees when the card
is posted from what they see the moment it is edited, with nothing raising.

A `Container`, `TextDisplay` or `Separator` cannot live in a plain
`discord.ui.View` — those are components-v2 items — so `LayoutView` is the
only view type built here. That is also why the card carries no message
`content`: a components-v2 message may not have any, and a components-v2
message can never be edited back to a classic view.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from daimon.adapters.discord.theme import COLOR_AMBER
from daimon.adapters.discord.views import check_control_invoker, finish_control
from daimon.core.posted_controls import CardButton, PostedCard

import discord

__all__ = ["build_card_view"]


class _ControlledCardView(discord.ui.LayoutView):
    """An invoker-bound card whose owner supplies authenticated control writes."""

    def __init__(self, allowed_user_id: int) -> None:
        super().__init__(timeout=None)
        self.allowed_user_id = allowed_user_id
        self._handled = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_control_invoker(
            interaction, allowed_user_id=self.allowed_user_id, handled=self._handled
        )

    def bind_control(
        self,
        button: discord.ui.Button[discord.ui.LayoutView],
        callback: Callable[[discord.Interaction], Awaitable[bool]],
    ) -> None:
        async def handle(interaction: discord.Interaction) -> None:
            # Discord checks before scheduling a callback; check again before
            # the first await so two accepted clicks cannot race the write.
            if not await self.interaction_check(interaction):
                return
            self._handled = True
            try:
                accepted = await callback(interaction)
            finally:
                # The owner may refuse on a fresh policy/version check. Its
                # refusal must not consume the card's controls.
                self._handled = False
            if accepted is not True:
                return
            self._handled = True
            for item in self.walk_children():
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            self.stop()
            if not interaction.response.is_done():
                await finish_control(interaction, view=self)

        button.callback = handle


def _build_card_button(button: CardButton) -> discord.ui.Button[discord.ui.LayoutView]:
    """One `CardButton` as a real button.

    A link button carries a url and nothing else; a form button carries the
    request token's `custom_id`, which is what the bot process matches its
    `DynamicItem` template against on a click.
    """
    if button.url is not None:
        return discord.ui.Button(label=button.label, url=button.url)
    return discord.ui.Button(
        style=discord.ButtonStyle.primary, label=button.label, custom_id=button.custom_id
    )


def _card_footer(card: PostedCard) -> str | None:
    """The card's footer as Discord renders it, or `None` when it has none.

    Only the `requested` footer is a template: it names the requester as a
    mention and the expiry as a short-time timestamp ("Expires 15:42." in the
    reader's own clock), both of which are Discord spellings core cannot
    produce. Every other state's footer is final text.
    """
    if card.footer is None:
        return None
    if card.state != "requested":
        return card.footer
    if card.requester_platform_user_id is None or card.expires_at_unix is None:
        raise ValueError("a requested card must carry a requester and an expiry")
    return card.footer.format(
        requester=f"<@{card.requester_platform_user_id}>",
        expires=f"<t:{card.expires_at_unix}:t>",
    )


def build_card_view(
    card: PostedCard,
    *,
    allowed_user_id: int | None = None,
    controls: Mapping[str, Callable[[discord.Interaction], Awaitable[bool]]] | None = None,
) -> discord.ui.LayoutView:
    """Render one `PostedCard` as the V2 `LayoutView` Discord posts and edits.

    Component order is a contract: the bolded headline, the facts as one
    subtext block, then — only when the card has buttons — one action row,
    then the footer as subtext. An invisible separator sits between each
    pair, so the title, body, buttons and footer each get their own gap.
    Every state wears the same amber accent: the headline emoji is the state
    marker, so nothing here ever turns green on success.

    Optional ``controls`` bind the non-link buttons to their authenticated
    owner, with the same invoker and single-click gate as ``CancelView``.
    That owner must recheck current authority and row version on every click;
    returning True means it accepted the write, never that a worker finished
    or its output passed review. False leaves the controls available after
    an owner-rendered refusal. Existing private forms omit these arguments.
    """
    if (allowed_user_id is None) != (controls is None):
        raise ValueError("bound card controls require their invoker")
    controlled = _ControlledCardView(allowed_user_id) if allowed_user_id is not None else None
    if controls is not None and set(controls) != {
        button.custom_id for button in card.buttons if button.url is None
    }:
        raise ValueError("bound card controls must match every non-link button")
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        accent_colour=discord.Colour(COLOR_AMBER)
    )
    container.add_item(discord.ui.TextDisplay(f"**{card.headline}**"))
    if card.facts:
        container.add_item(discord.ui.Separator(visible=False))
        container.add_item(discord.ui.TextDisplay("\n\n".join(f"-# {fact}" for fact in card.facts)))
    if card.buttons:
        container.add_item(discord.ui.Separator(visible=False))
        row_size = 5 if controlled is not None else len(card.buttons)
        for offset in range(0, len(card.buttons), row_size):
            action_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            for button in card.buttons[offset : offset + row_size]:
                item = _build_card_button(button)
                if controlled is not None and controls is not None and button.custom_id is not None:
                    controlled.bind_control(item, controls[button.custom_id])
                action_row.add_item(item)
            container.add_item(action_row)
    footer = _card_footer(card)
    if footer is not None:
        container.add_item(discord.ui.Separator(visible=False))
        container.add_item(discord.ui.TextDisplay(f"-# {footer}"))
    view = controlled if controlled is not None else discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view
