"""The Hand over button on a Teams conversation whose channel now answers with another agent."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.thread_handoff import VERB
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.task_continuations import get_continuation, list_pending_continuations
from daimon.core.teams_threads import conversation_of
from daimon.core.turn.errors import SessionAgentMismatch
from daimon.testing import ma_agent
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    FakeSender,
    TeamsApiFake,
    assert_card_renders,
    bot_token,
    build_teams_runtime,
    make_card_action,
    make_inbound,
    patched_admission,
    post_activity,
    running_service,
    settle,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
_DEFAULT = DeploymentDefault(agent_name="daimon", environment_name="default")


def _click() -> dict[str, object]:
    """A Hand over click on the notice, a reply in a channel thread."""
    invoke = make_card_action(VERB, "hand_over", agent="ag_research")
    invoke["conversation"] = {
        "id": THREAD_ID,
        "conversationType": "channel",
        "tenantId": ENTRA_TENANT_ID,
    }
    invoke["channelData"] = {
        "tenant": {"id": ENTRA_TENANT_ID},
        "channel": {"id": CHANNEL_ID},
        "team": {"id": "19:team@thread.tacv2"},
    }
    return invoke


async def _hand_over(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    *,
    policy: TenantAccessPolicy | None = None,
    click: dict[str, object] | None = None,
    channel_id: str = CHANNEL_ID,
    thread_id: str = THREAD_ID,
    clicks: int = 1,
    superseded: bool = False,
    seed: str | None = "Please finish the analysis of our experiment.",
) -> tuple[Any, str | None]:
    """Click Hand over; the invoke's answer and the thread's responder afterwards."""
    async with db_factory.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=TENANT, channel_id=channel_id),
            tenant_id=TENANT,
            agent_name="research-bot",
        )
        if policy is not None:
            await set_access_policy(session, tenant_id=TENANT, policy=policy)
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=TENANT))
    runtime = build_teams_runtime(
        db_factory, anthropic=build_fake_anthropic(router.dispatch), deployment_default=_DEFAULT
    )
    run_turn = AsyncMock()
    ready = asyncio.Event()
    original_dispatch = TeamsApp.dispatch_after_input

    async def dispatch(self: TeamsApp, *args: Any, **kwargs: Any) -> None:
        await ready.wait()
        await original_dispatch(self, *args, **kwargs)

    with (
        patch.object(TeamsApp, "_run_turn", run_turn),
        patch.object(TeamsApp, "dispatch_after_input", dispatch),
        patch.object(ThreadReader, "latest_user_message", AsyncMock(return_value=seed)),
    ):
        async with running_service(runtime, fake) as service:
            answer = await post_activity(service, click or _click())
            for _ in range(clicks - 1):
                await post_activity(service, click or _click())
            async with db_factory() as session:
                rows = await list_pending_continuations(
                    session, tenant_id=TENANT, platform="teams", thread_id=thread_id
                )
            if superseded:
                service.turns._last_message_at[conversation_of(thread_id)] = datetime.now(UTC)
            ready.set()
            await settle(service.turns)
    async with db_factory() as session:
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=TENANT, channel_id=channel_id, thread_id=thread_id, platform="teams"
            ),
            default=_DEFAULT,
        )
    if routed.thread_binding_id is None:
        assert rows == []
        run_turn.assert_not_awaited()
    else:
        assert len(rows) == 1, "one click or repeated clicks queue exactly one continuation"
        row = rows[0]
        assert row.reason == "task_handoff" and row.attended
        assert row.requester_external_user_id == AAD_OBJECT_ID
        assert row.target_ma_agent_id == "ag_research"
        assert row.requested_work == (seed or "Pick up this conversation where it left off.")
        async with db_factory() as session:
            settled = await get_continuation(session, idempotency_key=row.idempotency_key)
        assert settled is not None
        if superseded:
            run_turn.assert_not_awaited()
            assert settled.skip_reason == "skip_superseded"
        else:
            run_turn.assert_awaited_once()
            inbound = run_turn.await_args.args[0]
            assert row.requested_work in inbound.text and inbound.user_id == AAD_OBJECT_ID
            assert run_turn.await_args.kwargs["continuation"].target_ma_agent_id == "ag_research"
            assert settled.status == "delivered"
    return answer, routed.responder_ma_agent_id if routed.thread_binding_id else None


async def test_the_responder_changed_notice_offers_hand_over(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={},
        bot_token=bot_token,
    )
    mismatch = SessionAgentMismatch(
        mapping_id=uuid.uuid4(), session_id="s", source_agent_id="a", destination_agent_id="b"
    )
    with (
        patched_admission(),
        patch.object(app_module, "bind_session", AsyncMock(side_effect=mismatch)),
    ):
        await teams._run_turn(make_inbound("w"), TENANT)  # pyright: ignore[reportPrivateUsage]

    content = sender.activities[-1].model_dump(by_alias=True, exclude_none=True)["attachments"][0][
        "content"
    ]
    assert_card_renders(content)
    notice = json.dumps(content)
    assert "Switch to" in notice, "the button names the supported action"
    assert f'"verb": "{VERB}"' in notice, "the button routes to the hand-over handler"


@pytest.mark.parametrize("clicks", [1, 2])
async def test_a_click_hands_the_thread_to_the_channels_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    clicks: int,
) -> None:
    answer, responder = await _hand_over(db_session_factory, teams_api_fake, clicks=clicks)

    assert "handed this conversation over" in json.dumps(answer), "everyone sees who did it"
    assert '"verb"' not in json.dumps(answer), "the replaced card loses its button"
    assert responder == "ag_research", "later messages go to research-bot"


async def test_a_refused_click_answers_privately_and_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    answer, responder = await _hand_over(
        db_session_factory,
        teams_api_fake,
        policy=TenantAccessPolicy(protected_channel_ids=(CHANNEL_ID,)),
    )

    assert "Nothing was changed" in json.dumps(answer), "only the clicker is told why"
    assert responder is None, "no binding was written"


async def test_a_click_in_a_1_1_chat_hands_over_the_chat(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    _, responder = await _hand_over(
        db_session_factory,
        teams_api_fake,
        click=make_card_action(VERB, "hand_over", agent="ag_research"),
        channel_id=CONVERSATION_ID,
        thread_id=CONVERSATION_ID,
    )

    assert responder == "ag_research", "the chat itself is the conversation handed over"


async def test_a_channel_click_without_its_notice_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    click = _click()
    del click["replyToId"]
    answer, responder = await _hand_over(db_session_factory, teams_api_fake, click=click)

    assert "something went wrong" in json.dumps(answer), "no thread to name, so it fails"
    assert responder is None, "no binding was written"


async def test_a_human_message_after_the_click_supersedes_the_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
) -> None:
    await _hand_over(db_session_factory, teams_api_fake, superseded=True)


async def test_unreadable_history_uses_the_fixed_seed(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
) -> None:
    await _hand_over(db_session_factory, teams_api_fake, seed=None)
