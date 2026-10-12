"""Actual Discord controls over the shared resolver/operations and offline ports."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, create_autospec
from uuid import UUID

import discord
import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.posted_controls.fleet import (
    DiscordFleetCard,
    FleetControl,
    build_fleet_view,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import AgentRef, FleetFacts, Place, Subject
from daimon.core.fleet import FleetRun, FleetWorker, ResultPointer, result_digest
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetPorts
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.neutral_resources import WorkerLimits, WorkerPermissions
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, NetworkPolicy, ToolSpec
from mux.profiles import backend_selection, get_profile

NOW = datetime(2026, 10, 12, 19, tzinfo=UTC)
TENANT = derive_tenant_uuid(platform="discord", workspace_id="60")
ACCOUNT, RUN = UUID(int=41), UUID(int=42)
POLICY = TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini"))


def parent(provider="anthropic"):
    backend = backend_selection(provider)
    model = ModelRef(provider=provider, id=backend.model or "claude-haiku-5-5")
    profile = get_profile(backend.profile)
    tools = (ToolSpec(name="read", kind="custom", permission="auto"),)
    if profile.implicit_execution:
        tools += (ToolSpec(name=profile.execution_tool, kind="builtin"),)
    return WorkerPermissions(
        fleet=FleetFacts(
            scope=Scope(
                tenant_id=str(TENANT),
                account_id=str(ACCOUNT),
                principal_id="123",
                authorization_id="fixture-root-grant",
            ),
            root=ThreadRef(
                channel=ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="50"),
                thread_id="70",
            ),
            account_id=ACCOUNT,
            origin_id=UUID(int=43),
            run_id=RUN,
            place=Place(channel_id="70", parent_channel_id="50"),
            enabled=True,
            active=True,
        ),
        subject=Subject(platform_user_id="123"),
        agent=AgentRef.of("root"),
        spec=AgentSpec(name="root", model=model, system="Root safety", tools=tools),
        base_system="Root safety",
        backend_grants=(backend,),
        models=(model,),
        limits=WorkerLimits(
            budget_usd=Decimal("1"), deadline=NOW + timedelta(minutes=10), max_turns=4
        ),
        environment=EnvironmentSpec(name="root", network=NetworkPolicy(mode="none")),
        agent_id=UUID(int=44),
    )


def run():
    return FleetRun(
        RUN,
        TENANT,
        ACCOUNT,
        "root-session",
        "root-binding",
        "discord",
        "50",
        "70",
        "running",
        Decimal("1"),
        Decimal("0.2"),
        Decimal("0"),
        NOW + timedelta(minutes=10),
        8,
        None,
        2,
    )


def worker(provider="anthropic", ordinal=1, **updates):
    owner = parent(provider)
    row = FleetWorker(
        UUID(int=50 + ordinal),
        RUN,
        TENANT,
        ordinal,
        "research @everyone <#50>",
        owner.spec.model_dump(mode="json"),
        "fixture-digest",
        provider,
        owner.backend_grants[0].profile,
        owner.spec.model.id,
        "worker-binding",
        "pending",
        Decimal("0.2"),
        Decimal("0.2"),
        NOW + timedelta(minutes=5),
        2,
        0,
        1,
        None,
        None,
        None,
        None,
        3,
    )
    return replace(row, **updates)


def interaction(user_id=123, guild=60, thread=70, card=None):
    value = MagicMock(spec=discord.Interaction)
    value.user.id = user_id
    value.guild_id, value.channel_id = guild, thread
    value.channel = MagicMock(spec=discord.Thread)
    value.channel.send = AsyncMock()
    value.client.fleet_card_for = MagicMock(return_value=card)
    value.response.send_message = AsyncMock()
    value.response.edit_message = AsyncMock()
    value.response.is_done.return_value = False

    async def responded(*args, **kwargs):
        value.response.is_done.return_value = True

    value.response.send_message.side_effect = responded
    value.response.edit_message.side_effect = responded
    return value


def buttons(view):
    return [
        item
        for item in view.walk_children()
        if isinstance(item, FleetControl) and item.worker_id is not None
    ]


def setup(provider="anthropic"):
    owner = create_autospec(FleetPorts, instance=True)
    owner.read_run.return_value = (run(), (worker(provider),))
    owner.retained_control.return_value = None

    async def control(_parent, fleet, target, **kwargs):
        return {
            "run_id": str(fleet.id),
            "worker_id": str(target.id) if target is not None else None,
            "accepted": True,
            "version": kwargs["expected_version"] + 1,
            "action": kwargs["action"],
            "operation_key": kwargs["operation_key"],
            "expected_version": kwargs["expected_version"],
        }

    owner.control.side_effect = control
    loader = AsyncMock(return_value=(parent(provider), POLICY))
    send, edit = AsyncMock(), AsyncMock()
    card = DiscordFleetCard(
        run_id=RUN,
        service=FleetOrchestrator(owner),
        load_parent=loader,
        send=send,
        edit=edit,
        clock=lambda: NOW,
    )
    return card, owner, loader, send, edit


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("action", ["kill", "resume"])
async def test_sdk_button_uses_real_shared_control_and_current_host_loader(provider, action):
    card, owner, loader, send, _ = setup(provider)
    await card.refresh()
    view = send.call_args.kwargs["view"]
    label = "Stop 1" if action == "kill" else "Resume 1"
    click = interaction(card=card)
    await next(item for item in buttons(view) if item.item.label == label).callback(click)
    owner.control.assert_awaited_once()
    assert owner.control.call_args.kwargs["action"] == action
    assert owner.control.call_args.kwargs["expected_version"] == 3
    assert owner.control.call_args.args[0] is loader.return_value[0]
    assert loader.await_args_list[-1].args == (click,)
    click.response.edit_message.assert_awaited_once()
    assert str(RUN.hex) in buttons(view)[0].item.custom_id


@pytest.mark.parametrize(
    "mutation", ["wrong_user", "wrong_guild", "wrong_thread", "worker_origin", "inactive", "stale"]
)
async def test_real_core_refusal_before_any_control_write(mutation):
    card, owner, loader, send, _ = setup()
    await card.refresh()
    view = send.call_args.kwargs["view"]
    click = interaction(
        456 if mutation == "wrong_user" else 123,
        guild=61 if mutation == "wrong_guild" else 60,
        thread=71 if mutation == "wrong_thread" else 70,
        card=card,
    )
    if mutation in ("worker_origin", "inactive"):
        current, policy = loader.return_value
        loader.return_value = (
            replace(
                current,
                fleet=replace(current.fleet, depth=1)
                if mutation == "worker_origin"
                else replace(current.fleet, active=False),
            ),
            policy,
        )
    if mutation == "stale":
        owner.read_run.return_value = (run(), (worker(version=4),))
    await buttons(view)[0].callback(click)
    owner.control.assert_not_awaited()
    click.response.edit_message.assert_not_awaited()
    click.response.send_message.assert_awaited_once()


async def test_waiting_owner_ack_keeps_controls_active():
    card, owner, _, send, _ = setup()
    await card.refresh()
    view = send.call_args.kwargs["view"]
    original = owner.control.side_effect

    async def waiting(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["accepted"] = False
        return result

    owner.control.side_effect = waiting
    click = interaction(card=card)
    await buttons(view)[0].callback(click)
    assert not any(button.item.disabled for button in buttons(view))
    click.response.edit_message.assert_not_awaited()


@pytest.mark.parametrize("admin", [False, True])
async def test_current_nonrequester_subject_is_authorized_by_the_shared_owner(admin):
    card, owner, loader, send, _ = setup()
    await card.refresh()
    current, policy = loader.return_value
    loader.return_value = (
        replace(current, subject=Subject(platform_user_id="456", is_admin=admin)),
        policy,
    )
    await buttons(send.call_args.kwargs["view"])[0].callback(interaction(456, card=card))
    assert owner.control.await_count == (1 if admin else 0)


async def test_restart_dispatch_rebuilds_the_card_through_the_registered_sdk_item(
    db_session_factory, monkeypatch
):
    from .harness import make_bot
    from .test_bot import _make_runtime

    original, owner, loader, send, edit = setup()
    await original.refresh()
    view = send.call_args.kwargs["view"]
    custom_id = buttons(view)[0].item.custom_id
    components = view.to_components()
    del original, view

    def factory(thread, requester_id, _send, _edit, *, run_id=None):
        assert run_id == RUN
        return DiscordFleetCard(
            run_id=run_id,
            service=FleetOrchestrator(owner),
            load_parent=loader,
            send=send,
            edit=edit,
            clock=lambda: NOW,
        )

    runtime = _make_runtime(db_session_factory)
    runtime.settings.fleet.enabled = True
    bot = make_bot(replace(runtime, fleet_card_factory=factory))
    bot.start_orphan_recovery = MagicMock()
    await bot.setup_hook()
    click = interaction()
    click.client = bot
    click.channel.id = 70
    click.message = discord.Message(
        state=bot._connection,
        channel=click.channel,
        data={
            "id": "900",
            "type": 0,
            "content": "",
            "author": {"id": "999", "username": "fixture", "discriminator": "0", "avatar": None},
            "flags": 32768,
            "timestamp": "2026-10-12T19:00:00+00:00",
            "edited_timestamp": None,
            "pinned": False,
            "mention_everyone": False,
            "tts": False,
            "attachments": [],
            "embeds": [],
            "mentions": [],
            "mention_roles": [],
            "components": components,
        },
    )
    click.data = {"custom_id": custom_id, "component_type": 2}
    store = bot._connection._view_store
    tasks = []
    original_add = store.add_task

    def retain(task):
        tasks.append(task)
        original_add(task)

    monkeypatch.setattr(store, "add_task", retain)
    store.dispatch_dynamic_items(2, custom_id, click)
    assert len(tasks) == 1, "setup_hook must register the durable handler"
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
    owner.control.assert_awaited_once()
    assert owner.control.call_args.args[1].id == RUN
    assert owner.control.call_args.kwargs["expected_version"] == 3
    assert (
        owner.control.call_args.kwargs["operation_key"]
        == f"discord:fleet:{RUN}:{worker().id}:3:kill"
    )
    click.response.edit_message.assert_awaited_once()
    assert loader.await_args_list[-1].args == (click,)


async def test_the_default_maximum_fleet_keeps_every_worker_and_control():
    rows = tuple(worker(ordinal=index) for index in range(1, 9))
    view = build_fleet_view(run(), rows)
    assert len(buttons(view)) == 16
    assert view.total_children_count <= 40
    assert view.content_length() <= 4000


@pytest.mark.parametrize("terminal", [False, True])
async def test_sixteen_workers_fit_with_all_statuses_and_run_level_controls(terminal):
    rows = tuple(
        worker(ordinal=index, status="succeeded" if terminal and index > 12 else "pending")
        for index in range(1, 17)
    )
    view = build_fleet_view(replace(run(), max_workers=16), rows)
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert all(f"{index}. **" in text for index in range(1, 17))
    assert view.total_children_count <= 40
    assert view.content_length() <= 4000
    individual = buttons(view)
    assert len(individual) == 24
    expected = range(1, 13) if terminal else range(5, 17)
    assert {item.worker_id for item in individual} == {
        worker(ordinal=index).id for index in expected
    }
    all_controls = [
        item
        for item in view.walk_children()
        if isinstance(item, FleetControl) and item.worker_id is None
    ]
    assert {item.item.label for item in all_controls} == {"Stop all", "Resume all"}
    assert all(
        item.run_id == RUN and item.expected_version == run().version for item in all_controls
    )
    assert text.count("status only") == (0 if terminal else 4)


@pytest.mark.parametrize("action", ["kill", "resume"])
async def test_run_level_control_uses_the_same_owner_and_run_compare_and_set(action):
    card, owner, _, send, _ = setup()
    await card.refresh()
    view = send.call_args.kwargs["view"]
    control = next(
        item
        for item in view.walk_children()
        if isinstance(item, FleetControl) and item.worker_id is None and item.action == action
    )
    await control.callback(interaction(card=card))
    owner.control.assert_awaited_once()
    assert owner.control.call_args.args[2] is None
    assert owner.control.call_args.kwargs["expected_version"] == run().version
    assert owner.control.call_args.kwargs["operation_key"] == f"discord:fleet:{RUN}:all:2:{action}"


@pytest.mark.parametrize("enabled", [False, True])
async def test_setup_registers_fleet_control_only_when_enabled(db_session_factory, enabled):
    from .harness import make_bot
    from .test_bot import _make_runtime

    runtime = _make_runtime(db_session_factory)
    runtime.settings.fleet.enabled = enabled
    bot = make_bot(runtime)
    bot.start_orphan_recovery = MagicMock()
    await bot.setup_hook()
    registry = bot._connection._view_store._dynamic_items
    assert (registry.get(FleetControl.__discord_ui_compiled_template__) is FleetControl) is enabled


async def test_all_three_backends_render_without_spec_or_result_material():
    rows = tuple(
        worker(provider, i + 1, spec={"credential": "DO_NOT_RENDER_SECRET"})
        for i, provider in enumerate(("anthropic", "openai", "gemini"))
    )
    view = build_fleet_view(run(), rows)
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert all(provider in text for provider in ("anthropic", "openai", "gemini"))
    assert "DO_NOT_RENDER_SECRET" not in text and "root-session" not in text
    assert "@everyone" not in text and "<#50>" not in text
    assert len(buttons(view)) == 6


async def test_waiting_collect_blocks_real_lifecycle_summary_path():
    card, owner, _, send, edit = setup()
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    state = TurnState(content=[TextBlock(kind="text", text="A premature claimed summary")])
    await lifecycle.on_render(state)
    send.reset_mock()
    await lifecycle.on_terminal_success(state)
    owner.wait.assert_awaited_once()
    send.assert_not_awaited()
    assert not lifecycle.was_answered


@pytest.mark.parametrize("worker_status", [None, "pending", "failed"])
async def test_terminal_failure_sealed_answers_obey_fleet_disclosure_policy(worker_status):
    card, owner, _, send, edit = setup()
    owner.read_run.return_value = (run(), (worker(status=worker_status or "pending"),))
    lifecycle = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        agent_name="root",
        model_id="fixture-model",
        fleet_card=card if worker_status is not None else None,
    )
    sealed = "An unreviewed pre-tool answer. " * 30
    state = TurnState(
        content=[
            TextBlock(kind="text", text=sealed),
            ToolUseBlock(
                kind="tool_use", id="trailing-tool", type="agent.tool_use", name="read", input={}
            ),
        ]
    )
    await lifecycle.on_terminal_failure(state, RuntimeError("fixture worker failure"))
    messages = [
        str(call.kwargs.get("content", "")) for call in send.await_args_list + edit.await_args_list
    ]
    assert any(sealed in message for message in messages) is (worker_status is None)
    assert any("embeds" in call.kwargs for call in send.await_args_list + edit.await_args_list)
    owner.read_result.assert_not_awaited()
    owner.wait.assert_not_awaited()
    assert not lifecycle.was_answered


@pytest.mark.parametrize("verdict", ["approved", "refused"])
async def test_actual_review_barrier_controls_the_existing_summary_delivery(verdict):
    card, owner, _, send, edit = setup()

    def result(identifier, text):
        return Event(
            id=identifier,
            session_id=identifier,
            sequence=4,
            type="agent.message",
            turn_id="fixture-turn",
            observed_at=NOW,
            authority="record",
            native=NativeProvenance(provider="anthropic", api_revision="1"),
            payload={
                "item_id": identifier,
                "content": [{"type": "text", "text": text}],
                "complete": True,
            },
        )

    output = result("worker-result", "Verified primary result")
    digest = result_digest(dict(output.payload))
    review = result("review-result", json.dumps({"verdict": verdict, "reviewed_digest": digest}))
    primary = worker(
        status="succeeded",
        result_contract={"review_required": True},
        result=ResultPointer(output.session_id, 4, digest, "DO_NOT_POST_EXCERPT"),
    )
    reviewer = worker(
        ordinal=2,
        status="succeeded",
        reviewer_of=primary.id,
        result_contract={"format": "review", "reviewed_digest": digest},
        result=ResultPointer(
            review.session_id, 4, result_digest(dict(review.payload)), "DO_NOT_POST_REVIEW"
        ),
    )
    owner.read_run.return_value = (run(), (primary, reviewer))
    owner.read_result.side_effect = lambda _parent, _policy, _run, row: (
        output if row.id == primary.id else review
    )
    owner.validate_contract.return_value = "Verified primary result"
    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="root", model_id="fixture-model", fleet_card=card
    )
    await lifecycle.on_render(TurnState())
    send.reset_mock()
    state = TurnState(
        content=[TextBlock(kind="text", text="The orchestrator's one final summary.")]
    )
    await lifecycle.on_terminal_success(state)
    messages = [
        str(call.kwargs.get("content", "")) for call in send.await_args_list + edit.await_args_list
    ]
    final = [text for text in messages if "The orchestrator's one final summary." in text]
    assert len(final) == (1 if verdict == "approved" else 0)
    assert not any("DO_NOT_POST" in text or "reviewed_digest" in text for text in messages)
    assert lifecycle.was_answered is (verdict == "approved")


def test_real_bot_flag_off_does_not_construct_a_sender_or_fleet_service():
    from daimon.adapters.discord.bot import DaimonBot

    bot = MagicMock()
    bot.runtime.fleet_card_factory = MagicMock(side_effect=AssertionError("must stay cold"))
    bot.runtime.settings.fleet.enabled = False
    sender = MagicMock(side_effect=AssertionError("must stay cold"))
    assert (
        DaimonBot.fleet_card_for(
            bot, thread=MagicMock(), requester_id=123, send=sender, edit=AsyncMock()
        )
        is None
    )
    sender.assert_not_called()
    bot.runtime.fleet_card_factory.assert_not_called()
