"""DM routines through real admission, session assembly, prepared turns, the S1
gate and the Discord owner-DM poster, against fake Managed Agents and Discord."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from anthropic.types.beta.sessions.beta_managed_agents_agent_mcp_tool_use_event import (
    BetaManagedAgentsAgentMCPToolUseEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_requires_action import (
    BetaManagedAgentsSessionRequiresAction,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from daimon.adapters.discord.routine_delivery import make_discord_routine_poster
from daimon.core import direct_messages
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import DirectMessagePolicy
from daimon.core.direct_messages import (
    DirectMessageAccessRevoked,
    admit_cold_dm,
    reply_to_dm_result,
    run_dm_routine,
    start_cold_dm,
)
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.routine_delivery import poll_deliveries_once
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.direct_messages import (
    get_conversation,
    set_dm_agent_name,
    set_dm_enabled,
)
from daimon.core.stores.domain import Role, RoutineRow
from daimon.core.stores.routines import create_routine, get_routine, record_result
from daimon.core.stores.tenant_ledger import get_channel_spend
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn.admission import DmSource
from daimon.testing.factories import make_ledger_entry, make_tenant, make_tenant_config
from daimon.testing.ma import (
    FakeMAState,
    MARouter,
    build_fake_anthropic,
    combine_handlers,
    make_fake_memory_store_handler,
    not_found_response,
    sse_response,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.turn_fakes import deps_for
from daimon.testing.turn_router import turn_events
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD = 700000101
OWNER_A, OWNER_B = "999", "888"
ROUTE = "555"
BUDGET = "456"
SLACK_TOOLSET = {
    "type": "mcp_toolset",
    "mcp_server_name": "slack",
    "configs": [],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}
SLACK_SERVER = {"type": "url", "name": "slack", "url": "https://slack.example/mcp"}

Script = Callable[["Upstream", str], AsyncIterator[bytes]]


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk


class Upstream:
    """Managed Agents, as far as a turn sees it: sessions, events and streams.

    Each opened stream takes the next queued script (`answer` when none is
    queued). A stream keeps "running" until its script ends, so a turn that
    stops reading leaves the upstream turn going until something interrupts it.
    """

    def __init__(self, agent: Any, environment: Any) -> None:
        self.state = FakeSessionsState(FakeMAState())
        self.state.ma.agents[agent.id] = agent.model_dump(mode="json")
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.dead: set[str] = set()
        self.kill_first_send = False
        self.scripts: list[Script] = []
        self.changed = asyncio.Condition()
        roster = MARouter()
        roster.add_agent_list(agent)
        roster.add_environment_list(environment)
        roster.add_environment(environment)
        fallback = combine_handlers(
            make_fake_sessions_handler(self.state),
            make_fake_memory_store_handler(),
            roster.dispatch,
        )

        async def dispatch(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.strip("/").split("/")
            if request.method == "GET" and request.url.path == f"/v1/agents/{agent.id}":
                return httpx.Response(200, json=agent.model_dump(mode="json"))
            if request.method == "POST" and parts[-1] == "events" and parts[1] == "sessions":
                sid = parts[2]
                if self.kill_first_send and not self.dead:
                    self.dead.add(sid)
                if sid in self.dead:
                    return not_found_response(f"no such session: {sid}")
                body = json.loads(request.content)
                async with self.changed:
                    self.sent.extend((sid, event) for event in body["events"])
                    self.changed.notify_all()
            if request.method == "POST" and len(parts) == 3 and parts[1] == "sessions":
                self.updates.append((parts[2], json.loads(request.content)))
            if request.method == "GET" and request.url.path.endswith("/events/stream"):
                script = self.scripts.pop(0) if self.scripts else answer("private answer")
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_Stream(script(self, parts[2])),
                )
            return fallback(request)

        self.client = build_fake_anthropic(dispatch)

    def events(self, sid: str, kind: str) -> list[dict[str, Any]]:
        return [event for session, event in self.sent if session == sid and event["type"] == kind]

    async def wait(self, predicate: Callable[[], bool], timeout: float = 20) -> None:
        async with self.changed:
            await asyncio.wait_for(self.changed.wait_for(predicate), timeout)


def answer(text: str) -> Script:
    async def script(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
        events = turn_events(agent_text=text, event_id_suffix=f"_{sid}")
        upstream.state.events.setdefault(sid, []).extend(events)
        yield sse_response(events).content

    return script


async def hang(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
    """Accept the message and keep running until interrupted."""
    await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
    await upstream.wait(lambda: bool(upstream.events(sid, "user.interrupt")), timeout=60)
    yield b""


async def lost(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
    """The connection drops while the upstream turn carries on."""
    await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
    raise httpx.RemoteProtocolError("peer closed connection")
    yield b""  # pragma: no cover


async def pause_on_public_send(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
    """A third-party send that pauses, then whatever the confirmation decided."""
    await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
    now = datetime.now(UTC)
    pause = [
        BetaManagedAgentsAgentMCPToolUseEvent(
            id=f"tu_send_{sid}",
            type="agent.mcp_tool_use",
            name="send_message",
            input={"channel": "#general", "text": "my personal notes"},
            mcp_server_name="slack",
            processed_at=now,
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id=f"evt_pause_{sid}",
            type="session.status_idle",
            stop_reason=BetaManagedAgentsSessionRequiresAction(
                type="requires_action", event_ids=[f"tu_send_{sid}"]
            ),
            processed_at=now,
        ),
    ]
    yield sse_response([event.model_dump(mode="json") for event in pause]).content
    await upstream.wait(lambda: bool(upstream.events(sid, "user.tool_confirmation")))
    events = turn_events(agent_text="finished", event_id_suffix=f"_{sid}")
    upstream.state.events.setdefault(sid, []).extend(events)
    yield sse_response(events).content


class Harness:
    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant: Any, upstream: Upstream):
        self.factory = factory
        self.tenant = tenant
        self.upstream = upstream
        self.members: dict[str, bool] = {OWNER_A: True, OWNER_B: True}

        async def membership(_tenant: object, _guild: str, user_id: str) -> Membership:
            return Membership(is_member=self.members.get(user_id, False))

        self.resolver = CachedMembershipResolver(membership)
        self.gated: list[str] = []
        self.deps = replace(
            deps_for(upstream.client, factory),
            membership_resolver=self.resolver,
            turn_path="legacy",
        )

    def with_safety(self, policy: ToolSafetyPolicy) -> None:
        self.deps = replace(self.deps, tool_safety=policy)

    async def routine(
        self, owner: str = OWNER_A, **overrides: Any
    ) -> direct_messages.DmRoutineReply:
        return await run_dm_routine(
            self.deps,
            tenant_id=self.tenant.id,
            owner_user_id=owner,
            agent_name="daimon",
            prompt=overrides.get("prompt", "what is on my plate?"),
            source=overrides.get("source"),
            budget_channel_id=overrides.get("budget", BUDGET),
        )

    async def fire(self, row: RoutineRow) -> None:
        """What the scheduler does with a DM routine's run (`_fire_dm`)."""
        try:
            reply = await self.routine(
                row.created_by_user_id or "",
                source=DmSource(channel_id=row.source_channel_id, thread_id=row.source_thread_id)
                if row.source_channel_id is not None
                else None,
                budget=row.channel_id,
            )
        except Exception as error:
            async with self.factory.begin() as session:
                await record_result(
                    session, row.id, tail=None, error=getattr(error, "reason", str(error))
                )
            return
        async with self.factory.begin() as session:
            await record_result(
                session,
                row.id,
                tail=reply.text,
                delivery_payload=reply.text,
                error=None,
                delivery="pending",
                expected_destination=(row.destination_kind, row.destination_id),
            )

    async def live_dm(self, text: str, message_id: str, *, start: bool = True) -> str:
        if start:
            admission = await admit_cold_dm(
                self.deps,
                tenant_id=self.tenant.id,
                external_user_id=OWNER_A,
                channel_id=ROUTE,
                role=Role.USER,
                role_ids=(),
                verified_membership=True,
            )
            await start_cold_dm(
                self.deps,
                admission,
                tenant_id=self.tenant.id,
                workspace_id=self.tenant.external_id,
                route_key=ROUTE,
                external_user_id=OWNER_A,
            )
        conversation = await self.conversation()
        assert conversation is not None
        reply = await reply_to_dm_result(
            self.deps,
            platform="discord",
            route_key=ROUTE,
            external_user_id=OWNER_A,
            message_id=message_id,
            expected_scope_id=conversation.scope_id,
            text=text,
            role=Role.USER,
            verified_membership=True,
        )
        assert reply is not None
        return reply.text

    async def conversation(self):
        async with self.factory() as session:
            return await get_conversation(
                session, platform="discord", route_key=ROUTE, external_user_id=OWNER_A
            )

    async def live_session_id(self) -> str:
        conversation = await self.conversation()
        assert conversation is not None
        async with self.factory() as session:
            mapping = await get_live_thread_session(
                session,
                tenant_id=self.tenant.id,
                platform="discord",
                thread_id=conversation.scope_id,
                account_id=conversation.account_id,
            )
        assert mapping is not None
        return mapping.ma_session_id

    def manifest(self, sid: str) -> dict[str, Any]:
        """What a session runs with: memory, tools, servers, credentials."""
        session = self.upstream.state.sessions[sid]
        volatile = {"id", "created_at", "updated_at"}
        return {
            "resources": sorted(
                (
                    {k: v for k, v in r.model_dump(mode="json").items() if k not in volatile}
                    for r in self.upstream.state.resources.get(sid, [])
                ),
                key=json.dumps,
            ),
            "tools": [t.model_dump(mode="json") for t in session.agent.tools],
            "mcp_servers": [s.model_dump(mode="json") for s in session.agent.mcp_servers],
            "vault_ids": sorted(session.vault_ids),
            "agent": session.agent.id,
            "environment": session.environment_id,
        }

    def memory_stores(self, sid: str) -> dict[str, str]:
        return {
            resource.memory_store_id: getattr(resource, "access", "")
            for resource in self.upstream.state.resources.get(sid, [])
            if resource.type == "memory_store"
        }


@pytest.fixture
async def harness(
    db_engine: Any, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Harness]:
    db_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id=str(GUILD))
        await make_tenant_config(
            session, tenant=tenant, agent_name="daimon", environment_name="default"
        )
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("100"))
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="daimon")
    agent = ma_agent(
        id="ag_daimon",
        name="daimon",
        tenant_id=tenant.id,
        tools=[SLACK_TOOLSET],
        mcp_servers=[SLACK_SERVER],
    )
    environment = ma_environment(id="env_default", name="default", tenant_id=tenant.id)
    upstream = Upstream(agent, environment)
    built = Harness(db_session_factory, tenant, upstream)
    real_gate = direct_messages.gate_session_snapshot

    async def gate(client: Any, session: Any, *args: Any, **kwargs: Any) -> None:
        built.gated.append(session.id)
        await real_gate(client, session, *args, **kwargs)

    monkeypatch.setattr(direct_messages, "gate_session_snapshot", gate)
    try:
        yield built
    finally:
        await upstream.client.close()


@pytest.mark.parametrize("read_only", [False, True], ids=["writable", "dm_memory_read_only"])
async def test_routine_session_is_the_owners_live_dm_session(harness: Harness, read_only: bool):
    if read_only:
        async with harness.factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=harness.tenant.id,
                policy=TenantAccessPolicy(dm_memory_read_only=True),
            )
    assert await harness.live_dm("hello", "m1") == "private answer"
    live = await harness.live_session_id()
    routine_a = await harness.routine(OWNER_A)
    routine_b = await harness.routine(OWNER_B)

    assert routine_a.text == "private answer"
    assert harness.manifest(routine_a.session_id) == harness.manifest(live), (
        "same memory stores and modes, tools, servers and credentials as the live DM"
    )
    stores_a = harness.memory_stores(routine_a.session_id)
    stores_b = harness.memory_stores(routine_b.session_id)
    assert len(stores_a) == 2, "shared and personal memory are both mounted"
    assert len(set(stores_a) & set(stores_b)) == 1, "only the shared store is common"
    assert set(stores_a.values()) == {"read_only" if read_only else "read_write"}


@pytest.mark.parametrize(
    ("policy", "allowed"),
    [
        (ToolSafetyPolicy(enabled=True), False),
        (ToolSafetyPolicy(enabled=True, unattended_writes=("slack/send_message",)), True),
        (ToolSafetyPolicy(enabled=False), True),
    ],
    ids=["safety_on_refused", "allow_listed", "safety_off"],
)
async def test_a_paused_public_send_is_decided_unattended(
    harness: Harness, policy: ToolSafetyPolicy, allowed: bool
):
    harness.with_safety(policy)
    harness.upstream.scripts = [pause_on_public_send]
    reply = await harness.routine()

    (decision,) = harness.upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == ("allow" if allowed else "deny")
    assert reply.text == "finished", "the run goes on and reports back; it never waits"
    (toolset,) = harness.upstream.state.sessions[reply.session_id].agent.tools
    assert toolset.model_dump(mode="json")["default_config"]["permission_policy"] == {
        "type": "always_ask" if policy.enabled else "always_allow"
    }, "the session runs under the current policy's gate"
    assert harness.gated == [reply.session_id], "gated once, before the send"


async def test_cancel_after_recovery_stops_the_replacement_session(harness: Harness):
    harness.with_safety(ToolSafetyPolicy(enabled=True))
    harness.upstream.kill_first_send = True
    harness.upstream.scripts = [hang, hang]
    run = asyncio.create_task(harness.routine())
    upstream = harness.upstream
    await upstream.wait(
        lambda: any(
            event["type"] == "user.message" and sid not in upstream.dead
            for sid, event in upstream.sent
        )
    )
    (replacement,) = {sid for sid, event in upstream.sent if event["type"] == "user.message"}
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert upstream.events(replacement, "user.interrupt"), "the running replacement is stopped"
    assert replacement in harness.gated, "the replacement was gated before the send"
    assert harness.gated[-1] == replacement


async def _routine_row(harness: Harness) -> RoutineRow:
    async with harness.factory.begin() as session:
        return await create_routine(
            session,
            tenant_id=harness.tenant.id,
            created_by_user_id=OWNER_A,
            agent_id="ag_daimon",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="what is on my plate?",
            destination_kind="dm",
            channel_id=BUDGET,
        )


@pytest.mark.parametrize("failure", ["ceiling", "stream_lost"])
async def test_a_failed_run_stops_the_running_upstream_turn_and_queues_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, failure: str
):
    row = await _routine_row(harness)
    if failure == "ceiling":
        monkeypatch.setattr(
            direct_messages,
            "turn_deadline",
            lambda now: now + timedelta(seconds=3),
        )
        harness.upstream.scripts = [hang]
    else:
        harness.upstream.scripts = [lost, lost, lost]
    await harness.fire(row)

    (sid,) = {sid for sid, event in harness.upstream.sent if event["type"] == "user.message"}
    assert harness.upstream.events(sid, "user.interrupt"), "the upstream turn was stopped"
    async with harness.factory() as session:
        after = await get_routine(session, row.id, tenant_id=harness.tenant.id)
    assert after is not None and after.last_error
    assert after.delivery_status is None and after.delivery_payload is None


async def test_concurrent_live_dm_keeps_its_route_history_and_session(harness: Harness):
    await harness.live_dm("first", "m1")
    before = await harness.conversation()
    live = await harness.live_session_id()
    routine, second = await asyncio.gather(
        harness.routine(), harness.live_dm("second", "m2", start=False)
    )
    after = await harness.conversation()

    assert second == "private answer"
    assert after is not None and before is not None
    assert after.scope_id == before.scope_id, "the selected conversation is unchanged"
    assert [turn["text"] for turn in after.history if turn["role"] == "user"] == [
        "first",
        "second",
    ], "only the owner's own messages are in the history"
    assert await harness.live_session_id() == live, "the live DM keeps its session"
    assert routine.session_id != live


async def test_routine_answer_reaches_only_the_owners_dm_and_bills_its_budget(harness: Harness):
    row = await _routine_row(harness)
    await harness.fire(row)
    sent: list[tuple[int, int, str]] = []

    async def open_dm(guild_id: int, user_id: int) -> Any:
        class _Dm:
            async def send(self, *, content: str, allowed_mentions: object) -> None:
                sent.append((guild_id, user_id, content))

        return _Dm()

    async def no_channel(channel_id: int) -> object:
        raise AssertionError("a DM routine never posts to a channel")

    poster = make_discord_routine_poster(
        harness.factory,
        fetch_channel=no_channel,
        open_dm=open_dm,
        dm_policy=lambda _: DirectMessagePolicy(),
        membership_resolver=harness.resolver,
    )
    await poll_deliveries_once(
        harness.factory, platform="discord", post=poster, now=datetime.now(UTC)
    )

    assert [(guild, user) for guild, user, _ in sent] == [(GUILD, int(OWNER_A))]
    assert "private answer" in sent[0][2]
    async with harness.factory() as session:
        spent = await get_channel_spend(
            session, tenant_id=harness.tenant.id, channel_id=BUDGET, since=None, until=None
        )
    assert spent > 0, "the run is billed to the routine's budget channel"


async def test_revoked_owner_gets_no_run_and_no_delivery(harness: Harness):
    row = await _routine_row(harness)
    harness.members[OWNER_A] = False
    await harness.fire(row)
    assert not harness.upstream.sent, "nothing reached the model"
    async with harness.factory() as session:
        after = await get_routine(session, row.id, tenant_id=harness.tenant.id)
    assert after is not None and after.last_error and after.delivery_status is None


async def test_a_provider_native_agent_is_refused_before_any_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    real_admit = direct_messages.admit

    async def native_admit(*args: Any, **kwargs: Any) -> Any:
        admission = await real_admit(*args, **kwargs)
        revision = type("Revision", (), {"profile": "openai.responses"})()
        return replace(admission, backend_revision=revision, backend_revision_origin="agent")

    monkeypatch.setattr(direct_messages, "admit", native_admit)
    row = await _routine_row(harness)
    await harness.fire(row)
    assert not harness.upstream.state.sessions, "no session was created"
    async with harness.factory() as session:
        after = await get_routine(session, row.id, tenant_id=harness.tenant.id)
    assert after is not None
    assert after.last_error == "DM routines need a Managed Agents agent"
    assert after.delivery_status is None
    with pytest.raises(DirectMessageAccessRevoked):
        await harness.routine()
