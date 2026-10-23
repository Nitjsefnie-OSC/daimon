"""The Hand over button on a thread whose channel now answers with another agent.

Driven through the real `HandOverButton.callback` against real Postgres and an
MA fake: a member's click binds the thread to the channel's agent and queues
an attended turn; a refused click writes nothing and answers privately.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.continuation_dispatch import dispatch_pending_continuations
from daimon.adapters.discord.feedback_button import FeedbackButton
from daimon.adapters.discord.support_escalation import SupportEscalateButton
from daimon.adapters.discord.thread_handoff import HandOverButton, build_custom_id, hand_over_view
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import RecruitmentSettings, RecruitmentTenantSettings
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.task_continuations import get_continuation, list_pending_continuations
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from daimon.testing.turn_fakes import deps_for
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = "700000009"
_PARENT = 111111111111111111
_THREAD = 222222222222222222
_DEFAULT = DeploymentDefault(agent_name="daimon")


async def _seed(db_session: AsyncSession, *, policy: TenantAccessPolicy | None = None) -> Any:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=_GUILD)
    # The channel now answers with research-bot.
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=str(_PARENT)),
        tenant_id=tenant.id,
        agent_name="research-bot",
    )
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    return tenant


def _interaction(sessionmaker: async_sessionmaker[AsyncSession], tenant_id: Any) -> MagicMock:
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=tenant_id))
    runtime = SimpleNamespace(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessionmaker,
        turn_deps=replace(deps_for(None, None), deployment_default=_DEFAULT, turn_path=None),
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id = _THREAD
    thread.parent_id = _PARENT
    thread.send = AsyncMock()
    interaction = MagicMock()
    tasks: list[Any] = []
    run_turn = AsyncMock()

    async def dispatch(**kwargs: Any) -> None:
        await dispatch_pending_continuations(
            sessionmaker,
            runtime.anthropic,
            tenant_id=tenant_id,
            thread=thread,
            run_follow_up=run_turn,
            may_post=AsyncMock(return_value=True),
        )

    interaction.client = SimpleNamespace(
        runtime=runtime,
        application_id=77,
        _spawn=tasks.append,
        dispatch_continuations_in_thread=AsyncMock(side_effect=dispatch),
        tasks=tasks,
        run_turn=run_turn,
    )

    async def history(**kwargs: Any) -> AsyncIterator[Any]:
        yield SimpleNamespace(
            id=123,
            author=SimpleNamespace(id=42, bot=False),
            content="Please finish the analysis of our experiment.",
            created_at=datetime.now(UTC) - timedelta(minutes=1),
        )

    thread.history = history
    interaction.guild_id = int(_GUILD)
    interaction.channel = thread
    interaction.user = SimpleNamespace(id=42, mention="<@42>")
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=True)
    interaction.followup.send = AsyncMock()
    interaction.message.edit = AsyncMock()
    interaction.message.id = 456
    interaction.message.author.name = "Daimon"
    interaction.message.webhook_id = None
    interaction.message.application_id = None
    interaction.delete_original_response = AsyncMock()
    return interaction


@pytest.mark.parametrize("clicks", [1, 2])
async def test_a_click_hands_the_thread_to_the_channels_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], clicks: int
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)

    await HandOverButton(agent_id="ag_research").callback(interaction)

    interaction.channel.send.assert_awaited_once()
    posted = interaction.channel.send.call_args.args[0]
    assert "research-bot is picking up this conversation now" in posted
    interaction.message.edit.assert_awaited_once_with(view=None)
    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(_PARENT),
            thread_id=str(_THREAD),
        )
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id,
                channel_id=str(_PARENT),
                thread_id=str(_THREAD),
                platform="discord",
            ),
            default=_DEFAULT,
        )
    assert binding is not None and binding.kind == "handoff"
    assert routed.responder_ma_agent_id == "ag_research", "later messages go to research-bot"
    assert routed.thread_binding_kind == "handoff"

    if clicks == 2:
        interaction.user = SimpleNamespace(id=99, mention="<@99>")
        await HandOverButton(agent_id="ag_research").callback(interaction)
    async with db_session_factory() as session:
        rows = await list_pending_continuations(
            session, tenant_id=tenant.id, platform="discord", thread_id=str(_THREAD)
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.reason == "task_handoff" and row.attended
    assert row.requester_external_user_id == "42"
    assert row.requested_work == "Please finish the analysis of our experiment."
    await asyncio.gather(*interaction.client.tasks)
    interaction.client.run_turn.assert_awaited_once()
    dispatched_row, decision = interaction.client.run_turn.await_args.args
    assert dispatched_row.target_ma_agent_id == "ag_research"
    assert row.requested_work in decision.seed_user_message
    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=row.idempotency_key)
    assert settled is not None and settled.status == "delivered"


async def test_a_refused_click_answers_privately_and_writes_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(
        db_session, policy=TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)})
    )
    interaction = _interaction(db_session_factory, tenant.id)

    await HandOverButton(agent_id="ag_research").callback(interaction)

    interaction.channel.send.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert "only runs in other channels" in interaction.followup.send.call_args.args[0]
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(_PARENT),
            thread_id=str(_THREAD),
        )
    assert binding is None
    assert interaction.client.tasks == []
    async with db_session_factory() as session:
        assert (
            await list_pending_continuations(
                session, tenant_id=tenant.id, platform="discord", thread_id=str(_THREAD)
            )
            == []
        )


async def test_a_click_removes_a_webhook_cards_button(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)
    interaction.channel.parent = MagicMock(spec=discord.TextChannel)
    interaction.message.webhook_id = 123
    interaction.message.application_id = 77
    hook = MagicMock(spec=discord.Webhook)
    hook.edit_message = AsyncMock()

    with patch(
        "daimon.adapters.discord.post_transport.DiscordPostTransport._webhook",
        new_callable=AsyncMock,
        return_value=hook,
    ):
        await HandOverButton(agent_id="ag_research").callback(interaction)

    await asyncio.gather(*interaction.client.tasks)
    hook.edit_message.assert_awaited_once_with(
        interaction.message.id, view=None, thread=interaction.channel
    )
    interaction.message.edit.assert_not_awaited()


async def test_the_button_carries_the_agent_and_its_template_overlaps_no_other() -> None:
    view = hand_over_view(agent_id="agent_011CSabc", agent_name="research-bot")
    (button,) = view.children
    custom_id = build_custom_id("agent_011CSabc")
    assert getattr(button, "custom_id", None) == custom_id
    assert HandOverButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    for other in (FeedbackButton, SupportEscalateButton):
        assert other.__discord_ui_compiled_template__.fullmatch(custom_id) is None


async def test_a_human_message_after_the_click_supersedes_the_turn(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)
    await HandOverButton(agent_id="ag_research").callback(interaction)

    async def newer(**kwargs: Any) -> AsyncIterator[Any]:
        yield SimpleNamespace(author=SimpleNamespace(bot=False), created_at=datetime.now(UTC))

    interaction.channel.history = newer
    await asyncio.gather(*interaction.client.tasks)
    interaction.client.run_turn.assert_not_awaited()
    key = uuid.uuid5(tenant.id, f"handover:discord:{_PARENT}:{_THREAD}:ag_research:456")
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None and row.skip_reason == "skip_superseded"


async def test_a_reserved_thread_click_reads_and_starts_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)
    interaction.client.runtime.settings = SimpleNamespace(
        recruitment=RecruitmentSettings(
            tenants={
                _GUILD: RecruitmentTenantSettings(
                    channel_ids=(str(_PARENT),), recruiter_user_ids=("42",)
                )
            }
        )
    )
    interaction.channel.history = MagicMock(side_effect=AssertionError("must not read"))
    await HandOverButton(agent_id="ag_research").callback(interaction)
    assert interaction.client.tasks == []
    interaction.channel.history.assert_not_called()


async def test_unreadable_seed_history_uses_the_fixed_prompt(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    interaction = _interaction(db_session_factory, tenant.id)
    readable = interaction.channel.history
    interaction.channel.history = MagicMock(
        side_effect=discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "unreadable")
    )
    await HandOverButton(agent_id="ag_research").callback(interaction)
    interaction.channel.history = readable
    await asyncio.gather(*interaction.client.tasks)
    interaction.client.run_turn.assert_awaited_once()
    row, decision = interaction.client.run_turn.await_args.args
    assert row.requested_work == "Pick up this conversation where it left off."
    assert row.requested_work in decision.seed_user_message
