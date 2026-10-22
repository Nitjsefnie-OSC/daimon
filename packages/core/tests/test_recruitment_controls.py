"""Independent reader families refuse reserved channels before content/row access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.core.config import RecruitmentSettings


def policy():
    return RecruitmentSettings(tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}})


def channel():
    value = MagicMock(spec=discord.Thread)
    value.id, value.parent_id, value.guild.id = 20, 2, 101
    return value


def interaction():
    value = MagicMock(spec=discord.Interaction)
    value.channel, value.guild_id = channel(), 101
    value.client = SimpleNamespace(
        runtime=SimpleNamespace(settings=SimpleNamespace(recruitment=policy()))
    )
    value.response.is_done.return_value = False
    value.response.send_message = AsyncMock()
    value.followup.send = AsyncMock()
    return value


@pytest.mark.parametrize("entry", ["reconstruct", "authorize", "modal", "render", "submit"])
async def test_saved_wizard_refuses_before_loading_or_using_answers(entry, monkeypatch):
    from daimon.adapters.discord import wizard, wizard_submit

    tap = interaction()
    bot = tap.client
    load = AsyncMock(side_effect=AssertionError("wizard row/content"))
    monkeypatch.setattr(wizard, "_load_row", load)
    row = MagicMock()
    if entry == "reconstruct":
        assert await wizard._reconstruct_row(bot, "saved", tap, defer=False) == (None, True)
    elif entry == "authorize":
        assert not await wizard._authorize_tap(row, tap)
    elif entry == "modal":
        modal = wizard.WizardCustomTextModal(bot=bot, row=row, step_index=0, question="Synthetic")
        await modal.on_submit(tap)
    elif entry == "render":
        await wizard._apply_and_render(tap, bot=bot, row=row, action="next", values=[], text=None)
    else:
        await wizard_submit.run_wizard_submit_turn(
            bot=bot, interaction=tap, row=row, spec=MagicMock(), state=MagicMock()
        )
    load.assert_not_awaited()
    tap.response.send_message.assert_awaited_once()
    assert tap.response.send_message.await_args.kwargs["ephemeral"]


@pytest.mark.parametrize("kind", ["view", "modal"])
async def test_sdk_control_dispatch_refuses_before_dynamic_reconstruction(kind, monkeypatch):
    import asyncio

    from daimon.adapters.discord import control_dispatch

    tap = interaction()
    monkeypatch.setattr(control_dispatch.discord, "Interaction", MagicMock(return_value=tap))
    downstream = MagicMock(side_effect=AssertionError("SDK row/component loading"))
    tasks = []
    control_dispatch.dispatch_general_interaction(
        policy(),
        MagicMock(),
        {"type": 3 if kind == "view" else 5, "data": {"custom_id": "wizard:saved"}},
        downstream,
        lambda work: tasks.append(asyncio.create_task(work)),
    )
    await asyncio.gather(*tasks)
    downstream.assert_not_called()
    tap.response.send_message.assert_awaited_once_with("Source unavailable.", ephemeral=True)


async def test_raw_interaction_dispatch_blocks_all_general_controls_before_sdk(monkeypatch):
    import asyncio

    from daimon.adapters.discord import control_dispatch

    tap = interaction()
    monkeypatch.setattr(control_dispatch.discord, "Interaction", MagicMock(return_value=tap))
    dispatch = MagicMock(side_effect=AssertionError("SDK dispatch/row loading"))
    pending = []
    control_dispatch.dispatch_general_interaction(
        policy(),
        MagicMock(),
        {"data": {"name": "here"}},
        dispatch,
        lambda work: pending.append(asyncio.create_task(work)),
    )
    await asyncio.gather(*pending)
    dispatch.assert_not_called()
    tap.response.send_message.assert_awaited_once_with("Source unavailable.", ephemeral=True)
