"""Actual Discord controls over the shared resolver/operations and offline ports."""

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
    build_fleet_view,
    fleet_card,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import AgentRef, FleetFacts, Subject
from daimon.core.fleet import FleetRun, FleetWorker, ResultPointer, result_digest
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetPorts
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.neutral_resources import WorkerLimits, WorkerPermissions
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ToolSpec
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
        environment=EnvironmentSpec(name="root"),
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


def interaction(user_id=123, guild=60, thread=70):
    value = MagicMock(spec=discord.Interaction)
    value.user.id = user_id
    value.guild_id, value.channel_id = guild, thread
    value.response.send_message = AsyncMock()
    value.response.edit_message = AsyncMock()
    value.response.is_done.return_value = False

    async def responded(*args, **kwargs):
        value.response.is_done.return_value = True

    value.response.send_message.side_effect = responded
    value.response.edit_message.side_effect = responded
    return value


def buttons(view):
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def setup(provider="anthropic"):
    owner = create_autospec(FleetPorts, instance=True)
    owner.read_run.return_value = (run(), (worker(provider),))
    owner.retained_control.return_value = None

    async def control(_parent, fleet, target, **kwargs):
        return {
            "run_id": str(fleet.id),
            "worker_id": str(target.id),
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
        allowed_user_id=123,
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
    click = interaction()
    await next(item for item in buttons(view) if item.label == label).callback(click)
    owner.control.assert_awaited_once()
    assert owner.control.call_args.kwargs["action"] == action
    assert owner.control.call_args.kwargs["expected_version"] == 3
    assert owner.control.call_args.args[0] is loader.return_value[0]
    assert loader.await_args_list[-1].args == (click,)
    click.response.edit_message.assert_awaited_once()
    assert view.is_finished()


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
    assert not view.is_finished()


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
    click = interaction()
    await buttons(view)[0].callback(click)
    assert not view.is_finished()
    assert not any(button.disabled for button in buttons(view))
    click.response.edit_message.assert_not_awaited()


async def test_all_three_backends_render_without_spec_or_result_material():
    rows = tuple(
        worker(provider, i + 1, spec={"credential": "DO_NOT_RENDER_SECRET"})
        for i, provider in enumerate(("anthropic", "openai", "gemini"))
    )
    view = build_fleet_view(run(), rows, allowed_user_id=123, control=AsyncMock())
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


def test_flag_off_never_discovers_or_constructs_service():
    factory = MagicMock(side_effect=AssertionError("must stay cold"))
    assert fleet_card(factory=factory) is None
    assert fleet_card(enabled=True) is None
    factory.assert_not_called()


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
        DaimonBot._fleet_card_for(
            bot, thread=MagicMock(), requester_id=123, send=sender, edit=AsyncMock()
        )
        is None
    )
    sender.assert_not_called()
    bot.runtime.fleet_card_factory.assert_not_called()
