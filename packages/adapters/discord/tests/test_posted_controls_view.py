"""Component shape of the Discord posted-card renderer.

The card is the durable record of a private-value request, so what it draws
for each state is the contract: the button only ever exists while the request
is open, the state marker is the headline emoji rather than the accent, and
the `requested` footer is the one place a mention and an expiry are rendered
in Discord's own spellings.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.posted_controls import build_card_view
from daimon.adapters.discord.theme import COLOR_AMBER
from daimon.core.continuity.messages import ChangeAvailability, ConfigurationChange
from daimon.core.posted_controls import CardButton, CardState, PostedCard, build_posted_card

_EXPIRES_AT = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)
_REQUESTER = "100000000000000001"
_TOKEN = "tok-abcdefghijklmnopqrst"


def _requested_card(**overrides: Any) -> PostedCard:
    kwargs: dict[str, Any] = {
        "kind": "env",
        "state": "requested",
        "agent_name": "research-bot",
        "responder_name": "Daimon",
        "target": "TOGGL_TOKEN",
        "requester_platform_user_id": _REQUESTER,
        "expires_at": _EXPIRES_AT,
        "token": _TOKEN,
    }
    kwargs.update(overrides)
    return build_posted_card(**kwargs)


def _change(availability: ChangeAvailability) -> ConfigurationChange:
    return ConfigurationChange(
        target_name="research-bot", kind="key", detail="TOGGL_TOKEN", availability=availability
    )


def _applied_card() -> PostedCard:
    return _requested_card(
        state="applied",
        outcome=_change("next_message"),
    )


def _container(view: discord.ui.LayoutView) -> discord.ui.Container[discord.ui.LayoutView]:
    containers = [item for item in view.children if isinstance(item, discord.ui.Container)]
    assert len(containers) == 1, f"a card is exactly one container, got {view.children!r}"
    return containers[0]


def _text(view: discord.ui.LayoutView) -> list[str]:
    return [
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    ]


def _action_rows(view: discord.ui.LayoutView) -> list[discord.ui.ActionRow[Any]]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.ActionRow)]


def _buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button[Any]]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def _interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.is_done.return_value = False
    return interaction


async def test_bound_card_rejects_foreign_invoker_before_owner_write() -> None:
    card = _requested_card()
    write = AsyncMock(return_value=True)
    view = build_card_view(card, allowed_user_id=123, controls={card.buttons[0].custom_id: write})
    interaction = _interaction(456)
    await _buttons(view)[0].callback(interaction)
    write.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Only the command invoker can use these buttons.", ephemeral=True
    )
    assert not _buttons(view)[0].disabled


async def test_bound_card_serializes_two_preaccepted_clicks() -> None:
    card = _requested_card()
    started, release = asyncio.Event(), asyncio.Event()
    writes: list[int] = []

    async def write(interaction: discord.Interaction) -> bool:
        writes.append(interaction.user.id)
        started.set()
        await release.wait()
        return True

    view = build_card_view(card, allowed_user_id=123, controls={card.buttons[0].custom_id: write})
    first, second = _interaction(123), _interaction(123)
    assert await view.interaction_check(first)
    assert await view.interaction_check(second)
    task = asyncio.create_task(_buttons(view)[0].callback(first))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            await _buttons(view)[0].callback(second)
            second.response.send_message.assert_awaited_once_with(
                "Already handled.", ephemeral=True
            )
            release.set()
            await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert writes == [123]
    first.response.edit_message.assert_awaited_once_with(view=view)
    assert all(button.disabled for button in _buttons(view))
    assert view.is_finished()


@pytest.mark.parametrize("refusal", [False, None, {"status": "waiting"}, "approved", 1])
async def test_owner_refusal_keeps_controls_available_for_fresh_authorization(
    refusal: object,
) -> None:
    card = _requested_card()
    write = AsyncMock(side_effect=[refusal, True])
    view = build_card_view(card, allowed_user_id=123, controls={card.buttons[0].custom_id: write})
    first, second = _interaction(123), _interaction(123)
    await _buttons(view)[0].callback(first)
    assert not view.is_finished()
    assert not _buttons(view)[0].disabled
    first.response.edit_message.assert_not_awaited()
    await _buttons(view)[0].callback(second)
    assert write.await_count == 2
    assert view.is_finished()


@pytest.mark.parametrize("error", [RuntimeError("write unavailable"), asyncio.CancelledError()])
async def test_failed_owner_write_does_not_strand_the_control(error: BaseException) -> None:
    card = _requested_card()
    write = AsyncMock(side_effect=error)
    view = build_card_view(card, allowed_user_id=123, controls={card.buttons[0].custom_id: write})
    with pytest.raises(type(error)):
        await _buttons(view)[0].callback(_interaction(123))
    assert await view.interaction_check(_interaction(123))
    assert not view.is_finished()
    assert not _buttons(view)[0].disabled


def test_bound_controls_require_invoker_and_exact_button_coverage() -> None:
    card = _requested_card()
    for kwargs in (
        {"allowed_user_id": 123},
        {"controls": {}},
        {"allowed_user_id": 123, "controls": {}},
        {"allowed_user_id": 123, "controls": {"another-card": AsyncMock()}},
    ):
        with pytest.raises(ValueError):
            build_card_view(card, **kwargs)


def test_many_worker_controls_fit_discord_rows_without_dropping_any() -> None:
    original = _requested_card()
    card = original.model_copy(
        update={
            "buttons": tuple(
                CardButton(label=f"Stop {index}", style="primary", custom_id=f"worker:{index}")
                for index in range(8)
            )
        }
    )
    with pytest.raises(ValueError):
        build_card_view(card)
    view = build_card_view(
        card,
        allowed_user_id=123,
        controls={f"worker:{index}": AsyncMock(return_value=True) for index in range(8)},
    )
    assert [len(row.children) for row in _action_rows(view)] == [5, 3]
    assert [button.custom_id for button in _buttons(view)] == [
        f"worker:{index}" for index in range(8)
    ]


def test_requested_card_renders_headline_facts_button_and_footer_in_order() -> None:
    card = _requested_card()
    view = build_card_view(card)

    texts = _text(view)
    assert texts[0] == f"**{card.headline}**", "the headline leads the card, bolded"
    assert texts[1] == "\n\n".join(f"-# {fact}" for fact in card.facts), (
        "every fact renders as one subtext block, in the core-owned order"
    )
    rows = _action_rows(view)
    assert len(rows) == 1, "a requested card carries exactly one action row"
    buttons = _buttons(view)
    assert len(buttons) == 1, f"exactly one button on a requested card, got {buttons!r}"
    assert buttons[0].custom_id == f"ztc:{_TOKEN}", (
        "the button must carry the core-built request custom_id the bot dispatches on"
    )
    assert buttons[0].style is discord.ButtonStyle.primary, "the form button is the primary action"


def test_title_body_buttons_and_footer_each_get_their_own_gap() -> None:
    kinds = [
        type(item).__name__ for item in _container(build_card_view(_requested_card())).children
    ]
    assert kinds == [
        "TextDisplay",
        "Separator",
        "TextDisplay",
        "Separator",
        "ActionRow",
        "Separator",
        "TextDisplay",
    ], "an invisible separator sits between the title, the body, the buttons and the footer"
    multi = _requested_card(state="refused", refusal="token_rejected")
    (body,) = _text(build_card_view(multi))[1:]
    assert "\n\n" in body and "-# " in body.split("\n\n")[1], "facts sit a blank line apart"


def test_requested_footer_renders_the_requester_mention_and_the_expiry_time() -> None:
    view = build_card_view(_requested_card())

    footer = _text(view)[-1]
    assert footer.startswith("-# "), "the footer renders as subtext"
    assert f"<@{_REQUESTER}>" in footer, "the footer names the requester as a real mention"
    assert f"Expires <t:{int(_EXPIRES_AT.timestamp())}:t>." in footer, (
        "the expiry renders as Discord's short-time timestamp (Expires 15:42.), "
        "in the reader's own clock, never a formatted string"
    )
    assert "{requester}" not in footer and "{expires}" not in footer, (
        "no template placeholder may survive into the rendered card"
    )


def test_applied_card_drops_the_action_row_and_leads_with_the_success_marker() -> None:
    view = build_card_view(_applied_card())

    assert _action_rows(view) == [], "an applied card must offer nothing left to click"
    assert _buttons(view) == [], "an applied card must carry no button"
    assert _text(view)[0].startswith("**✅"), "the applied state is marked by the headline emoji"


def test_link_and_form_buttons_coexist_in_one_action_row() -> None:
    card = _requested_card().model_copy(
        update={
            "buttons": (
                CardButton(
                    label="🔐 Enter it privately", style="primary", custom_id=f"ztc:{_TOKEN}"
                ),
                CardButton(label="What is this?", style="link", url="https://example.com/help"),
            )
        }
    )
    view = build_card_view(card)

    assert len(_action_rows(view)) == 1, "both buttons share one action row"
    buttons = _buttons(view)
    assert [b.style for b in buttons] == [
        discord.ButtonStyle.primary,
        discord.ButtonStyle.link,
    ], "button order and style follow the card's own button tuple"
    assert buttons[1].url == "https://example.com/help", "a link button carries its url"
    assert buttons[1].custom_id is None, "a link button is not dispatchable and carries no token"


def test_card_view_is_components_v2_and_never_times_out() -> None:
    view = build_card_view(_requested_card())

    assert view.has_components_v2(), (
        "the card must post as a components-v2 message; a classic view can never be "
        "edited onto it afterwards"
    )
    assert view.timeout is None, "a posted card outlives the process that built it"


@pytest.mark.parametrize(
    "card",
    [
        pytest.param(_requested_card(), id="requested"),
        pytest.param(_requested_card(state="received"), id="received"),
        pytest.param(_applied_card(), id="applied"),
        pytest.param(
            _requested_card(
                state="partial",
                outcome=_change("preparation_failed"),
            ),
            id="partial",
        ),
        pytest.param(
            _requested_card(state="refused", refusal="replacement_admin_required"), id="refused"
        ),
        pytest.param(_requested_card(state="expired"), id="expired"),
        pytest.param(_requested_card(state="superseded"), id="superseded"),
    ],
)
def test_every_card_state_wears_the_same_amber_accent(card: PostedCard) -> None:
    accent = _container(build_card_view(card)).accent_colour

    assert accent is not None and accent.value == COLOR_AMBER, (
        f"state {card.state!r} must keep the single card accent — the headline emoji is the "
        "state marker, so nothing ever turns green"
    )


@pytest.mark.parametrize(
    "state", ["applied", "partial", "refused", "expired", "superseded"], ids=str
)
def test_no_terminal_state_leaves_a_clickable_button(state: CardState) -> None:
    outcome = _change("next_message") if state in ("applied", "partial") else None
    refusal = "replacement_admin_required" if state == "refused" else None
    view = build_card_view(_requested_card(state=state, outcome=outcome, refusal=refusal))

    assert _buttons(view) == [], f"state {state!r} must not leave a button a click can reach"
