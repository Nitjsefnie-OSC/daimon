"""The Hand over button on a Slack thread whose channel now answers with another agent."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aioresponses import CallbackResult
from daimon.adapters.slack import thread_handoff
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.continuation_dispatch import dispatch_pending_continuations
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.thread_handoff import HAND_OVER_ACTION_ID, handle_hand_over_click
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.task_continuations import get_continuation, list_pending_continuations
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

_TEAM = "T_HANDOVER"
_CHANNEL = "C_PARENT"
_THREAD_TS = "1700000000.000100"
_DEFAULT = DeploymentDefault(agent_name="daimon")


def _payload() -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM},
        "user": {"id": "U_CLICKER", "team_id": _TEAM},
        "channel": {"id": _CHANNEL},
        "container": {"channel_id": _CHANNEL, "message_ts": "1700000000.000200"},
        "message": {"ts": "1700000000.000200", "thread_ts": _THREAD_TS, "text": "notice"},
        "actions": [{"action_id": HAND_OVER_ACTION_ID, "value": "ag_research"}],
    }


async def _click(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: TenantAccessPolicy | None = None,
    clicks: int = 1,
    superseded: bool = False,
    external: bool = False,
    seed_read_error: bool = False,
) -> Any:
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=_CHANNEL),
        tenant_id=tenant.id,
        agent_name="research-bot",
    )
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=tenant.id))
    runtime = SimpleNamespace(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        deployment_default=_DEFAULT,
    )

    async def _client(_runtime: Any, *, team_id: str) -> Any:
        assert team_id == _TEAM
        return fake_slack_web_client.client

    fake_slack_web_client.mock.clear()
    for method in ("chat.postMessage", "chat.update", "chat.postEphemeral"):
        fake_slack_web_client.mock.post(
            f"https://slack.com/api/{method}", payload={"ok": True}, repeat=True
        )
    fake_slack_web_client.mock.get(
        re.compile(r"https://slack.com/api/users.info.*"),
        payload={"ok": True, "user": {"is_admin": False}},
        repeat=True,
    )

    def replies(url: URL, **kwargs: Any) -> CallbackResult:
        if seed_read_error and "latest" in url.query:
            return CallbackResult(payload={"ok": False, "error": "missing_scope"})
        messages = [
            {
                "user": "U_CLICKER",
                "ts": _THREAD_TS,
                "text": "Please finish the analysis of our experiment.",
            },
            {"user": "U_OTHER", "ts": "1700000000.000150", "text": "other work"},
        ]
        if "oldest" in url.query:
            messages = (
                [
                    {
                        "user": "U_OTHER",
                        "text": "actually stop",
                        "ts": str((datetime.now(UTC) + timedelta(seconds=1)).timestamp()),
                    }
                ]
                if superseded
                else []
            )
        return CallbackResult(payload={"ok": True, "messages": messages, "has_more": False})

    fake_slack_web_client.mock.get(
        re.compile(r"https://slack.com/api/conversations.replies.*"),
        callback=replies,
        repeat=True,
    )
    tasks: list[Any] = []
    run_turn = AsyncMock()

    async def dispatch(**kwargs: Any) -> None:
        await dispatch_pending_continuations(
            db_session_factory,
            runtime.anthropic,
            fake_slack_web_client.client,
            tenant_id=tenant.id,
            channel=_CHANNEL,
            thread_id=_THREAD_TS,
            active_turn=False,
            run_follow_up=run_turn,
        )

    app = SimpleNamespace(
        _spawn=tasks.append, dispatch_continuations_in_thread=AsyncMock(side_effect=dispatch)
    )
    monkeypatch.setattr(thread_handoff, "resolve_web_client", _client)
    payload = _payload()
    if external:
        payload["user"]["team_id"] = "T_FOREIGN"
    for index in range(clicks):
        if index:
            payload["user"]["id"] = "U_SECOND_CLICKER"
        await handle_hand_over_click(cast(SlackRuntime, runtime), payload, app=cast(SlackApp, app))
    async with db_session_factory() as session:
        rows = await list_pending_continuations(
            session, tenant_id=tenant.id, platform="slack", thread_id=_THREAD_TS
        )
    await asyncio.gather(*tasks)
    return SimpleNamespace(id=tenant.id, rows=rows, run_turn=run_turn, tasks=tasks)


def _sent(fake_slack_web_client: Any, method: str) -> list[dict[str, Any]]:
    return [
        req.kwargs["json"]
        for (_m, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL(f"https://slack.com/api/{method}")
        for req in reqs
    ]


@pytest.mark.parametrize("clicks", [1, 2])
async def test_a_click_hands_the_thread_to_the_channels_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    clicks: int,
) -> None:
    tenant = await _click(
        db_session, db_session_factory, fake_slack_web_client, monkeypatch, clicks=clicks
    )

    (posted,) = _sent(fake_slack_web_client, "chat.postMessage")
    assert posted["thread_ts"] == _THREAD_TS
    assert "research-bot is picking up this conversation now" in posted["text"]
    (updated,) = _sent(fake_slack_web_client, "chat.update")
    assert updated["blocks"] == [], "the notice loses its button"
    async with db_session_factory() as session:
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id, channel_id=_CHANNEL, thread_id=_THREAD_TS, platform="slack"
            ),
            default=_DEFAULT,
        )
    assert routed.responder_ma_agent_id == "ag_research", "later messages go to research-bot"
    assert routed.thread_binding_kind == "handoff"
    assert len(tenant.rows) == 1
    row = tenant.rows[0]
    assert row.attended and row.reason == "task_handoff"
    assert row.requester_external_user_id == "U_CLICKER"
    assert row.requested_work == "Please finish the analysis of our experiment."
    tenant.run_turn.assert_awaited_once()
    dispatched, seed = tenant.run_turn.await_args.args
    assert dispatched.target_ma_agent_id == "ag_research" and row.requested_work in seed
    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=row.idempotency_key)
    assert settled is not None and settled.status == "delivered"


async def test_a_refused_click_answers_privately_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _click(
        db_session,
        db_session_factory,
        fake_slack_web_client,
        monkeypatch,
        policy=TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)),
    )

    assert _sent(fake_slack_web_client, "chat.postMessage") == []
    (ephemeral,) = _sent(fake_slack_web_client, "chat.postEphemeral")
    assert ephemeral["user"] == "U_CLICKER"
    assert "Nothing was changed" in ephemeral["text"]
    async with db_session_factory() as session:
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id, channel_id=_CHANNEL, thread_id=_THREAD_TS, platform="slack"
            ),
            default=_DEFAULT,
        )
    assert routed.thread_binding_id is None
    assert tenant.rows == [] and tenant.tasks == []


async def test_a_human_message_after_the_click_supersedes_the_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _click(
        db_session, db_session_factory, fake_slack_web_client, monkeypatch, superseded=True
    )
    result.run_turn.assert_not_awaited()
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=result.rows[0].idempotency_key)
    assert row is not None and row.skip_reason == "skip_superseded"


async def test_a_slack_connect_click_queues_and_dispatches_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _click(
        db_session, db_session_factory, fake_slack_web_client, monkeypatch, external=True
    )
    assert result.rows == [] and result.tasks == []
    result.run_turn.assert_not_awaited()


async def test_unreadable_seed_history_uses_the_fixed_prompt(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _click(
        db_session, db_session_factory, fake_slack_web_client, monkeypatch, seed_read_error=True
    )
    result.run_turn.assert_awaited_once()
    row, seed = result.run_turn.await_args.args
    assert row.requested_work == "Pick up this conversation where it left off."
    assert row.requested_work in seed
