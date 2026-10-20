"""Callback ingress through the production Discord entry and mux driver, offline."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.core.continuity.wakes import poll_wakes_once
from daimon.core.stores import watches
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn.prepare import PreparedTurn
from daimon.testing import ma_agent
from daimon.testing.ma import (
    build_fake_anthropic,
    list_response,
    send_events_response,
    sse_response,
)

from .harness import make_bot
from .test_continuation_dispatch_entry import _make_guild, _make_member, _make_runtime, _make_thread
from .watch_fixtures import (
    cached_watch_session,
    enrolled_page_watch,
    watch_admission_patches,
    watch_router,
)


@pytest.mark.fresh_schema
@pytest.mark.parametrize("mode", ["ready", "revoked_owner", "deleted", "policy_revoked"])
async def test_callback_to_bound_discord_mux_turn(db_session_factory, mode, monkeypatch):
    db_session_factory, now, tenant, principal, mapping, watch, key = await enrolled_page_watch(
        db_session_factory, monkeypatch
    )
    target = ma_agent(
        id="ag_target",
        name="target-agent",
        tenant_id=tenant.id,
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            },
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "configs": [
                    {
                        "name": "get_issue",
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                ],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            },
        ],
        mcp_servers=[{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}],
    )
    snapshot = await cached_watch_session(db_session_factory, target, mapping)
    sent = []
    user_sent = asyncio.Event()
    updates = []
    sent_tools = []
    original_tools = snapshot.agent.tools
    original_servers = snapshot.agent.mcp_servers
    router = watch_router(target, tenant.id, lambda: snapshot)

    def update(request, _match):
        nonlocal snapshot
        body = json.loads(request.content)
        updates.append(body)
        if "agent" in body:
            merged = snapshot.agent.model_dump(mode="json") | body["agent"]
            snapshot = snapshot.model_copy(
                update={"agent": type(snapshot.agent).model_validate(merged)}
            )
        return httpx.Response(200, json=snapshot.model_dump(mode="json"))

    def send(request, _match):
        sent.extend(json.loads(request.content)["events"])
        sent_tools.append(snapshot.agent.tools)
        user_sent.set()
        return send_events_response()

    router.add("POST", r"/v1/sessions/sess_watch", update)
    router.add("POST", r"/v1/sessions/sess_watch/events", send)
    router.add("GET", r"/v1/sessions/sess_watch/events", lambda *_: list_response([]))

    class AfterSend(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.wait_for(user_sent.wait(), timeout=5)
            yield sse_response(
                [
                    {
                        "id": f"evt_watch_report_{len(sent_tools)}",
                        "type": "agent.message",
                        "content": [{"type": "text", "text": "The upload is ready."}],
                        "processed_at": now.isoformat(),
                    },
                    {
                        "id": f"evt_watch_idle_{len(sent_tools)}",
                        "type": "session.status_idle",
                        "stop_reason": {"type": "end_turn"},
                        "processed_at": now.isoformat(),
                    },
                ]
            ).content

    router.add(
        "GET",
        r"/v1/sessions/sess_watch/events/stream",
        lambda *_: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=AfterSend()
        ),
    )
    transport_failures = []
    requests = []

    def dispatch(request):
        requests.append((request.method, request.url.path))
        try:
            return router.dispatch(request)
        except Exception as exc:
            transport_failures.append(repr(exc))
            raise

    fake_client = build_fake_anthropic(dispatch)
    runtime = _make_runtime(db_session_factory, agent=target)
    deps = replace(
        runtime.turn_deps,
        anthropic=fake_client,
        turn_path="mux",
        tool_safety=ToolSafetyPolicy(enabled=True),
    )
    runtime = replace(runtime, anthropic=fake_client, turn_deps=deps)
    bot = make_bot(runtime)
    guild = _make_guild(member=_make_member(admin=False))
    if mode == "revoked_owner":
        guild = _make_guild()
    guild.id = 710000099
    thread = _make_thread(guild=guild)
    thread.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages_in_threads=True
    )
    bot.get_channel = MagicMock(return_value=thread)
    if mode == "deleted":
        bot.get_channel = MagicMock(return_value=None)
        bot.fetch_channel = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404, reason="gone"), "gone")
        )
    if mode == "policy_revoked":
        from daimon.core.access_policy import TenantAccessPolicy
        from daimon.core.stores.access_policy import set_access_policy

        async with db_session_factory.begin() as db:
            await set_access_policy(
                db,
                tenant_id=tenant.id,
                policy=TenantAccessPolicy(invoker_user_ids=("someone_else",)),
            )

    bound = []

    async def bind(_deps, admission, **_kwargs):
        async def record(**_event):
            pass

        prepared = PreparedTurn(
            admission=admission,
            ma_session_id="sess_watch",
            mapping_id=mapping.id,
            watermark=None,
            reused=True,
            session_account_id=principal.account_id,
            _record=record,
        )
        bound.append(prepared)
        return prepared

    with watch_admission_patches(bind):
        for _ in range(2):
            await poll_wakes_once(
                db_session_factory,
                platform="discord",
                open_thread=bot._open_wake_thread,
                now=datetime.now(UTC),
            )
            for task in list(bot._bg_tasks):
                await task
    async with db_session_factory() as db:
        wake = await get_continuation(db, idempotency_key=key)
        assert len(await watches.list_evidence(db, watch.id)) == 1
    assert wake is not None
    if mode == "ready":
        assert wake.status == "delivered", wake
        assert len([event for event in sent if event["type"] == "user.message"]) == 1, (
            transport_failures,
            requests,
        )
        assert "Summarize the upload" in str(sent) and 'trust="untrusted"' in str(sent)
        assert updates and "always_ask" in str(updates)
        assert "Summarize the upload" in str(sent)
        assert thread.send.await_count > 0
        thread.history.assert_not_called()
        assert snapshot.agent.tools == original_tools
        assert snapshot.agent.mcp_servers == original_servers
        assert sent_tools[0] != original_tools

        # An attended turn reusing the same prepared session/thread must get its
        # ordinary tools, even without another bind or agent-config change.
        from daimon.core.turn.run import run_prepared_turn
        from daimon.testing.turn_fakes import RecordingLifecycle

        user_sent.clear()
        await run_prepared_turn(
            deps,
            replace(bound[0], admission=replace(bound[0].admission, observation=None)),
            tenant_id=tenant.id,
            platform="discord",
            thread_id="4242",
            external_user_id="555",
            user_message="An attended follow-up.",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=AsyncMock(return_value="An attended follow-up."),
            recovery_lifecycle=MagicMock(),
            attended=True,
            operation_key="attended-follow-up",
        )
        assert len(sent_tools) == 2
        assert sent_tools[1] == original_tools
    else:
        assert wake.status == "skipped", wake
        assert sent == []
        if mode == "deleted":
            assert wake.skip_reason == "thread_unavailable"
            thread.send.assert_not_awaited()
