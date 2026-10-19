"""DM routines through real admission, session assembly, prepared turns, the S1
gate and the Discord owner-DM poster, against fake Managed Agents and Discord."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from secrets import randbits
from types import SimpleNamespace
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
from cryptography.fernet import Fernet
from daimon.adapters.discord.routine_delivery import make_discord_routine_poster
from daimon.adapters.scheduler.main import _build_fire, _CapsAdapter
from daimon.core import direct_messages
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.direct_messages import (
    admit_cold_dm,
    reply_to_dm_result,
    start_cold_dm,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.mcp_vault import add_external_mcp_credential
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.mux_backend import resource_scope
from daimon.core.routine_delivery import poll_deliveries_once
from daimon.core.routine_sessions import RoutineSessionSupervisor
from daimon.core.scheduler import RoutineDispatcher, run_one_tick
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import routine_sessions as lifecycle_store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.credential_requests import create_credential_request
from daimon.core.stores.direct_messages import (
    get_conversation,
    set_dm_agent_name,
    set_dm_enabled,
)
from daimon.core.stores.domain import Role, RoutineRow, RoutineSessionRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.mcp_oauth_flows import create_flow, mark_flow_completed
from daimon.core.stores.routines import create_routine, get_routine
from daimon.core.stores.tenant_ledger import get_channel_spend
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.stores.worker_ownership import hold_worker_owner
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn.deps import build_turn_deps
from daimon.testing.db import build_test_engine
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
from daimon.testing.turn_router import turn_events
from sqlalchemy import text
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
        self.crypto_key = Fernet.generate_key()
        self.crypto = Fernet(self.crypto_key)
        self.vaults: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, dict[str, dict[str, Any]]] = {}
        self.encrypted_tokens: dict[str, bytes] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.dead: set[str] = set()
        self.kill_first_send = False
        self.interrupt_entered = asyncio.Event()
        self.interrupt_gate: asyncio.Event | None = None
        self.interrupt_error = False
        self.discovery_error = False
        self.create_pause_at: int | None = None
        self.create_entered = asyncio.Event()
        self.create_gate = asyncio.Event()
        self.creates = 0
        self.file_listing_error = False
        self.memory_outage = False
        self.download_hook: Any = None
        self.pause_call = (
            "slack",
            "send_message",
            {"channel": "#general", "text": "my personal notes"},
        )
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
            if self.memory_outage and "memory_stores" in request.url.path:
                return httpx.Response(
                    503, json={"error": {"type": "api_error", "message": "mount offline"}}
                )
            if self.download_hook is not None and request.url.path.endswith("/content"):
                await self.download_hook()
            if len(parts) >= 2 and parts[1] == "vaults":
                return self.vault_request(request, parts)
            if (
                request.method == "GET"
                and request.url.path == "/v1/sessions"
                and self.discovery_error
            ):
                return httpx.Response(
                    503, json={"error": {"type": "api_error", "message": "offline"}}
                )
            if (
                request.method == "GET"
                and request.url.path.endswith("/files")
                and self.file_listing_error
            ):
                return httpx.Response(
                    503, json={"error": {"type": "api_error", "message": "offline"}}
                )
            if request.method == "POST" and request.url.path == "/v1/sessions":
                response = fallback(request)
                self.creates += 1
                if self.creates == self.create_pause_at:
                    self.create_entered.set()
                    await self.create_gate.wait()
                return response
            if request.method == "GET" and request.url.path == f"/v1/agents/{agent.id}":
                return httpx.Response(200, json=agent.model_dump(mode="json"))
            if request.method == "POST" and parts[-1] == "events" and parts[1] == "sessions":
                sid = parts[2]
                if self.kill_first_send and not self.dead:
                    self.dead.add(sid)
                if sid in self.dead:
                    return not_found_response(f"no such session: {sid}")
                body = json.loads(request.content)
                kinds = {event["type"] for event in body["events"]}
                if "user.interrupt" in kinds:
                    self.interrupt_entered.set()
                    if self.interrupt_gate is not None:
                        await self.interrupt_gate.wait()
                    if self.interrupt_error:
                        return httpx.Response(
                            503, json={"error": {"type": "api_error", "message": "offline"}}
                        )
                self.state.sessions[sid].status = "idle" if "user.interrupt" in kinds else "running"
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

    def vault_request(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        now = "2026-10-19T00:00:00Z"
        if len(parts) == 2:
            if request.method == "GET":
                return httpx.Response(
                    200, json={"data": list(self.vaults.values()), "has_more": False}
                )
            body = json.loads(request.content)
            vid = f"vlt_{len(self.vaults)}"
            vault = {
                "id": vid,
                "type": "vault",
                "created_at": now,
                "archived_at": None,
                "metadata": body.get("metadata"),
                "display_name": body["display_name"],
            }
            self.vaults[vid] = vault
            self.credentials[vid] = {}
            return httpx.Response(200, json=vault)
        vid = parts[2]
        if parts[-1] == "credentials" and request.method == "GET":
            return httpx.Response(
                200, json={"data": list(self.credentials[vid].values()), "has_more": False}
            )
        if request.method == "DELETE":
            self.credentials[vid].pop(parts[-1])
            return httpx.Response(200, json={"id": parts[-1], "type": "vault_credential_deleted"})
        body = json.loads(request.content)
        cid = parts[-1] if len(parts) == 5 else f"vcrd_{uuid.uuid4().hex}"
        auth = dict(body["auth"])
        token = auth.pop("token", None)
        if token is not None:
            self.encrypted_tokens[cid] = self.crypto.encrypt(token.encode())
        credential = {
            "id": cid,
            "type": "vault_credential",
            "vault_id": vid,
            "metadata": body.get("metadata") or {},
            "auth": auth,
            "created_at": now,
            "updated_at": now,
        }
        self.credentials[vid][cid] = credential
        return httpx.Response(200, json=credential)

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
    yield sse_response(
        [
            event
            for event in turn_events(agent_text="", event_id_suffix=f"_stop_{sid}")
            if event["type"] == "session.status_idle"
        ]
    ).content


async def lost(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
    """The connection drops while the upstream turn carries on."""
    await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
    if upstream.events(sid, "user.interrupt"):
        yield sse_response(
            [
                event
                for event in turn_events(agent_text="", event_id_suffix=f"_stop_{sid}")
                if event["type"] == "session.status_idle"
            ]
        ).content
    else:
        raise httpx.RemoteProtocolError("peer closed connection")


async def pause_on_public_send(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
    """A third-party send that pauses, then whatever the confirmation decided."""
    await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
    now = datetime.now(UTC)
    pause = [
        BetaManagedAgentsAgentMCPToolUseEvent(
            id=f"tu_send_{sid}",
            type="agent.mcp_tool_use",
            name=upstream.pause_call[1],
            input=upstream.pause_call[2],
            mcp_server_name=upstream.pause_call[0],
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
        self.settings = Settings(
            _env_file=None,
            database={"url": factory.kw["bind"].url.render_as_string(hide_password=False)},
            mcp={"public_url": "https://mcp.example/mcp", "jwt_secret": "t" * 32},
            crypto={"keys": [upstream.crypto_key.decode()]},
            turn={"path": "mux"},
        )
        self.deps = replace(
            build_turn_deps(
                self.settings,
                upstream.client,
                factory,
                deployment_default=DeploymentDefault(),
                resolver_cache=new_resolver_cache(),
                billing_config=None,
            ),
            membership_resolver=self.resolver,
        )
        self.supervisor = RoutineSessionSupervisor(factory, upstream.client, owner_key=randbits(63))
        self.dispatcher = RoutineDispatcher(2, supervisor=self.supervisor)

    def with_safety(self, policy: ToolSafetyPolicy) -> None:
        self.deps = replace(self.deps, tool_safety=policy)
        self.settings = self.settings.model_copy(update={"tool_safety": policy})

    async def routine(self, owner: str = OWNER_A, **overrides: Any) -> Any:
        row = await _routine_row(self, owner=owner)
        await self.fire(row)
        async with self.factory() as db:
            after = await get_routine(db, row.id, tenant_id=row.tenant_id)
            intents = [
                item for item in await lifecycle_store.pending(db) if item.routine_id == row.id
            ]
        assert after is not None
        if after.last_error:
            raise RuntimeError(after.last_error)
        assert not intents
        session_id = next(
            sid
            for sid, session in self.upstream.state.sessions.items()
            if session.metadata.get("daimon_routine_session")
            in {str(intent) for intent in self.completed_intents}
        )
        return SimpleNamespace(
            text=after.last_result_tail, session_id=session_id, routine_id=row.id
        )

    @property
    def completed_intents(self) -> set[Any]:
        # The provider receipt token identifies this fire independently of scope.
        return self._last_intents

    async def fire(self, row: RoutineRow) -> None:
        fire = await _build_fire(
            client=self.upstream.client,
            sm=self.factory,
            settings=self.settings,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            membership_resolver=self.resolver,
            supervisor=self.supervisor,
        )
        try:
            await run_one_tick(
                now=datetime.now(UTC),
                sm=self.factory,
                caps=_CapsAdapter(self.factory, billing_config=None),
                fire=fire,
                max_age=timedelta(minutes=5),
                max_concurrent_fires=2,
                dispatch_timeout_s=120,
                dispatcher=self.dispatcher,
                wait_for_completion=True,
            )
        finally:
            await asyncio.shield(self.supervisor.drain())
        self._last_intents = {
            intent.id for intent in await self.intents(row) if intent.state == "completed"
        }

    async def intents(self, row: RoutineRow) -> list[RoutineSessionRow]:
        async with self.factory() as db:
            return await lifecycle_store.list_for_routine(
                db, routine_id=row.id, tenant_id=row.tenant_id
            )

    async def retry_now(self, row: RoutineRow) -> None:
        async with self.factory.begin() as db:
            await db.execute(
                text(
                    "UPDATE routine_sessions SET next_attempt_at = :due "
                    "WHERE routine_id = :routine AND tenant_id = :tenant"
                ),
                {
                    "due": datetime.now(UTC) - timedelta(seconds=1),
                    "routine": row.id,
                    "tenant": row.tenant_id,
                },
            )

    async def boot_recover(self, row: RoutineRow) -> RoutineSessionSupervisor:
        await self.supervisor.abort_pending()
        if self.owner_context is not None:
            await self.owner_context.__aexit__(None, None, None)
            self.owner_context = None
        await self.retry_now(row)
        recovery = RoutineSessionSupervisor(
            self.factory, self.upstream.client, owner_key=randbits(63)
        )
        self.supervisor = recovery
        self.dispatcher = RoutineDispatcher(2, supervisor=recovery)
        self.owner_context = hold_worker_owner(
            self.factory.kw["bind"], owner_key=recovery.owner_key, ownership_lost=asyncio.Event()
        )
        await self.owner_context.__aenter__()
        # The next scheduled slot is still in the future. A real scheduler
        # tick recovers pending obligations without recreating/resending work.
        await self.fire(row)
        return recovery

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
        from daimon.core.turn.unattended import protected_tools

        tools = [tool.model_dump(mode="json") for tool in session.agent.tools]
        for tool in tools:
            if tool.get("mcp_server_name") == "daimon-mcp":
                # Routines additionally force unattended protected actions to
                # ask. Compare all remaining capability/permission fields.
                tool["configs"] = [
                    config
                    for config in tool.get("configs", [])
                    if config["name"] not in protected_tools(self.deps.tool_safety)
                ]
        return {
            "resources": sorted(
                (
                    {k: v for k, v in r.model_dump(mode="json").items() if k not in volatile}
                    for r in self.upstream.state.resources.get(sid, [])
                ),
                key=json.dumps,
            ),
            "tools": tools,
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
    db_engine: Any, db_schema: str, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Harness]:
    execution_engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False),
        db_schema,
        pool_size=6,
        max_overflow=0,
        pool_timeout=10,
    )
    db_session_factory = async_sessionmaker(execution_engine, expire_on_commit=False)
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
        tools=[
            SLACK_TOOLSET,
            *[
                {**SLACK_TOOLSET, "mcp_server_name": name}
                for name in ("private_a", "private_b", "daimon-mcp")
            ],
        ],
        mcp_servers=[
            SLACK_SERVER,
            {"type": "url", "name": "private_a", "url": "https://a.example/mcp"},
            {"type": "url", "name": "private_b", "url": "https://b.example/mcp"},
            {"type": "url", "name": "daimon-mcp", "url": "https://mcp.example/mcp"},
        ],
    )
    environment = ma_environment(id="env_default", name="default", tenant_id=tenant.id)
    upstream = Upstream(agent, environment)
    built = Harness(db_session_factory, tenant, upstream)
    built.accounts = {}
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    for owner, url in ((OWNER_A, "https://a.example/mcp"), (OWNER_B, "https://b.example/mcp")):
        async with db_session_factory.begin() as db:
            principal = await get_or_create_platform_principal(
                db, tenant_id=tenant.id, platform="discord", external_id=owner
            )
            built.accounts[owner] = principal.account_id
            state = f"grant_{owner}_{uuid.uuid4().hex}"
            await create_credential_request(
                db,
                token=state,
                kind="mcp",
                tenant_id=tenant.id,
                account_id=principal.account_id,
                agent_id=agent_uuid,
                target=f"private_{owner}",
                mcp_server_url=url,
                requester_platform_user_id=owner,
                channel_id=ROUTE,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                idempotency_key=uuid.uuid4(),
                target_ma_agent_id=agent.id,
                target_name=agent.name,
                requested_work=None,
            )
            await create_flow(
                db,
                state=state,
                request_token=state,
                tenant_id=tenant.id,
                account_id=principal.account_id,
                agent_id=agent_uuid,
                server_name=f"private_{owner}",
                mcp_server_url=url,
                redirect_uri="https://mcp.example/oauth",
                code_verifier="verifier",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await mark_flow_completed(db, state=state, now=datetime.now(UTC))
        await add_external_mcp_credential(
            upstream.client,
            account_id=principal.account_id,
            agent_id=agent_uuid,
            mcp_server_url=url,
            token=f"personal-secret-{owner}",
            jwt_secret=b"t" * 32,
            public_url="https://mcp.example/mcp",
            now=datetime.now(UTC),
            session_factory=db_session_factory,
            scope=resource_scope(
                tenant_id=str(tenant.id),
                account_id=str(principal.account_id),
                authorization_id="test-personal-connect",
            ),
        )
    built.owner_context = hold_worker_owner(
        execution_engine, owner_key=built.supervisor.owner_key, ownership_lost=asyncio.Event()
    )
    await built.owner_context.__aenter__()
    real_gate = direct_messages.gate_session_snapshot

    async def gate(client: Any, session: Any, *args: Any, **kwargs: Any) -> None:
        built.gated.append(session.id)
        await real_gate(client, session, *args, **kwargs)

    monkeypatch.setattr(direct_messages, "gate_session_snapshot", gate)
    try:
        yield built
    finally:
        await built.dispatcher.close()
        if built.owner_context is not None:
            await built.owner_context.__aexit__(None, None, None)
        await upstream.client.close()
        await execution_engine.dispose()


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
    toolset = next(
        tool
        for tool in harness.upstream.state.sessions[reply.session_id].agent.tools
        if tool.mcp_server_name == "slack"
    )
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


async def _routine_row(harness: Harness, *, owner: str = OWNER_A) -> RoutineRow:
    async with harness.factory.begin() as session:
        return await create_routine(
            session,
            tenant_id=harness.tenant.id,
            created_by_user_id=owner,
            agent_id="ag_daimon",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="what is on my plate?",
            destination_kind="dm",
            channel_id=BUDGET,
            source_provenance_known=True,
            next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
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
            lambda now: now + timedelta(seconds=20),
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
    with pytest.raises(RuntimeError, match="Managed Agents"):
        await harness.routine()


async def _accepted_run(
    harness: Harness, *, replacement: bool = False
) -> tuple[RoutineRow, asyncio.Task[None], str]:
    row = await _routine_row(harness)
    harness.upstream.kill_first_send = replacement
    harness.upstream.scripts = [hang, hang] if replacement else [hang]
    task = asyncio.create_task(harness.fire(row))
    await harness.upstream.wait(
        lambda: any(event["type"] == "user.message" for _, event in harness.upstream.sent)
    )
    sid = next(sid for sid, event in harness.upstream.sent if event["type"] == "user.message")
    return row, task, sid


@pytest.mark.parametrize("replacement", [False, True], ids=["initial", "replacement"])
async def test_shutdown_stops_every_allocation_and_leaves_no_owned_tasks(
    harness: Harness, replacement: bool
):
    row, task, sid = await _accepted_run(harness, replacement=replacement)
    await harness.dispatcher.close()
    await asyncio.gather(task, return_exceptions=True)
    intents = await harness.intents(row)
    assert len(intents) == (2 if replacement else 1)
    assert {intent.state for intent in intents} == {"stopped"}
    assert len({intent.id for intent in intents}) == len(intents)
    assert all(intent.finished_at is not None for intent in intents)
    assert harness.upstream.events(sid, "user.interrupt")
    assert not harness.supervisor._tasks
    async with harness.factory() as db:
        after = await get_routine(db, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.delivery_payload is None


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_cancel_failed_stop_then_boot_retries_the_same_session(
    harness: Harness, failure: str
):
    row, task, sid = await _accepted_run(harness)
    if failure == "error":
        harness.upstream.interrupt_error = True
    else:
        harness.upstream.interrupt_gate = asyncio.Event()
    await harness.dispatcher.close()
    await asyncio.gather(task, return_exceptions=True)
    (pending,) = await harness.intents(row)
    assert pending.state == "stopping" and pending.stop_attempts == 1
    assert pending.ma_session_id == sid and pending.finished_at is None
    harness.upstream.interrupt_error = False
    harness.upstream.interrupt_gate = None
    creates = harness.upstream.creates
    recovery = await harness.boot_recover(row)
    (after,) = await harness.intents(row)
    assert after.id == pending.id and after.ma_session_id == sid
    assert after.state == "stopped" and after.stop_attempts == 2
    assert harness.upstream.creates == creates, "recovery never recreates or resends the prompt"
    assert len(harness.upstream.events(sid, "user.message")) == 1
    assert not recovery._tasks


@pytest.mark.parametrize("replacement", [False, True], ids=["initial", "replacement"])
async def test_crash_after_create_before_receipt_discovers_the_intent(
    harness: Harness, replacement: bool
):
    row = await _routine_row(harness)
    upstream = harness.upstream
    upstream.kill_first_send = replacement
    upstream.create_pause_at = 2 if replacement else 1
    upstream.discovery_error = True
    task = asyncio.create_task(harness.fire(row))
    await asyncio.wait_for(upstream.create_entered.wait(), 30)
    # The provider has committed creation, but the SDK has no returned ID.
    await harness.dispatcher.close()
    await asyncio.gather(task, return_exceptions=True)
    intents = await harness.intents(row)
    pending = intents[-1]
    assert pending.ma_session_id is None and pending.mapping_id is None
    sid = next(
        sid
        for sid, session in upstream.state.sessions.items()
        if session.metadata.get("daimon_routine_session") == str(pending.id)
    )
    upstream.discovery_error = False
    await harness.boot_recover(row)
    recovered = (await harness.intents(row))[-1]
    assert recovered.id == pending.id and recovered.ma_session_id == sid
    assert recovered.state == "stopped"
    assert upstream.creates == upstream.create_pause_at
    assert not upstream.events(sid, "user.message"), (
        "an ambiguous allocation is stopped, never resent"
    )


@pytest.mark.parametrize("exit_kind", ["error", "cancel"])
async def test_repeated_cancellation_cannot_cancel_an_owned_stop(harness: Harness, exit_kind: str):
    row = await _routine_row(harness)
    upstream = harness.upstream
    upstream.interrupt_gate = asyncio.Event()
    upstream.scripts = [lost, lost, lost] if exit_kind == "error" else [hang]
    fire = asyncio.create_task(harness.fire(row))
    await upstream.wait(lambda: any(event["type"] == "user.message" for _, event in upstream.sent))
    if exit_kind == "cancel":
        for task in list(harness.dispatcher._tasks.values()):
            task.cancel()
    await asyncio.wait_for(upstream.interrupt_entered.wait(), 30)
    close = asyncio.create_task(harness.dispatcher.close())
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.sleep(0)
    close.cancel()
    upstream.interrupt_gate.set()
    results = await asyncio.gather(close, fire, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    (intent,) = await harness.intents(row)
    assert intent.state == "stopped" and intent.stop_attempts == 1
    assert not harness.supervisor._tasks


async def test_live_owner_is_not_recovered_by_a_competing_sweep(harness: Harness):
    row, task, sid = await _accepted_run(harness)
    other = RoutineSessionSupervisor(
        harness.factory, harness.upstream.client, owner_key=randbits(63)
    )
    await other.recover()
    assert not harness.upstream.events(sid, "user.interrupt")
    (intent,) = await harness.intents(row)
    assert intent.state == "running" and intent.stop_attempts == 0
    await harness.dispatcher.close()
    await asyncio.gather(task, return_exceptions=True)
    assert (await harness.intents(row))[0].state == "stopped"


async def test_delivery_failure_keeps_a_successful_session_completed(harness: Harness):
    row = await _routine_row(harness)
    await harness.fire(row)
    (before,) = await harness.intents(row)
    assert before.state == "completed"

    async def unavailable(guild: int, owner: int) -> Any:
        raise LookupError("DM transport down")

    async def no_channel(channel: int) -> Any:
        raise AssertionError("no public fallback")

    poster = make_discord_routine_poster(
        harness.factory,
        fetch_channel=no_channel,
        open_dm=unavailable,
        dm_policy=lambda _: DirectMessagePolicy(),
        membership_resolver=harness.resolver,
    )
    await poll_deliveries_once(
        harness.factory, platform="discord", post=poster, now=datetime.now(UTC)
    )
    (after,) = await harness.intents(row)
    assert after.state == "completed" and after.finished_at == before.finished_at
    assert not harness.upstream.events(after.ma_session_id or "", "user.interrupt")


@pytest.mark.parametrize("allowed", [True, False], ids=["owner_receives", "policy_refuses"])
async def test_exhaustion_survives_a_later_fire_and_notifies_only_the_owner(
    harness: Harness, allowed: bool
):
    row, task, sid = await _accepted_run(harness)
    harness.upstream.interrupt_error = True
    await harness.dispatcher.close()
    await asyncio.gather(task, return_exceptions=True)
    await harness.boot_recover(row)
    await harness.retry_now(row)
    await harness.fire(row)
    (unresolved,) = await harness.intents(row)
    assert unresolved.state == "stop_unresolved" and unresolved.stop_attempts == 3
    immutable = unresolved.finished_at
    harness.upstream.interrupt_error = False
    # A later successful fire changes last_error/result, never this outcome.
    harness.dispatcher = RoutineDispatcher(2, supervisor=harness.supervisor)
    async with harness.factory.begin() as db:
        from daimon.core.stores.routines import update_routine

        await update_routine(
            db,
            row.id,
            tenant_id=row.tenant_id,
            next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    await harness.fire(row)
    async with harness.factory() as db:
        after = await get_routine(db, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.last_error is None
    assert after.session_lifecycle.stop_unresolved == 1 and after.session_lifecycle.pending == 0
    (old, new) = await harness.intents(row)
    assert old.state == "stop_unresolved" and old.finished_at == immutable
    assert new.state == "completed" and old.id != new.id
    sent: list[tuple[int, int, str]] = []

    async def dm(guild: int, owner: int) -> Any:
        class Destination:
            async def send(self, *, content: str, allowed_mentions: object) -> None:
                sent.append((guild, owner, content))

        return Destination()

    async def no_channel(channel: int) -> Any:
        raise AssertionError("a stop notice has no public fallback")

    policy = DirectMessagePolicy() if allowed else DirectMessagePolicy(mode="disabled")
    poster = make_discord_routine_poster(
        harness.factory,
        fetch_channel=no_channel,
        open_dm=dm,
        dm_policy=lambda _: policy,
        membership_resolver=harness.resolver,
    )
    await poll_deliveries_once(
        harness.factory, platform="discord", post=poster, now=datetime.now(UTC)
    )
    notices = [
        item
        for item in sent
        if item[2] == "This run may still be running; Daimon couldn't confirm it stopped."
    ]
    assert notices == (
        [
            (
                GUILD,
                int(OWNER_A),
                "This run may still be running; Daimon couldn't confirm it stopped.",
            )
        ]
        if allowed
        else []
    )
    before = len(sent)
    await poll_deliveries_once(
        harness.factory, platform="discord", post=poster, now=datetime.now(UTC)
    )
    assert len(sent) == before, "the durable notice claim prevents repeated sends"
    assert harness.upstream.events(sid, "user.message")


async def test_attached_owner_credentials_are_nonempty_distinct_and_scope_checked(harness: Harness):
    import jwt
    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier, token_row_refusal
    from daimon.adapters.mcp.runtime import McpRuntime
    from daimon.adapters.mcp.tools.routines import _get_routine_impl
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.stores.mcp_tokens import get_mcp_token
    from fastmcp.exceptions import ToolError

    a = await harness.routine(OWNER_A)
    b = await harness.routine(OWNER_B)
    sessions = harness.upstream.state.sessions
    (vault_a,) = sessions[a.session_id].vault_ids
    (vault_b,) = sessions[b.session_id].vault_ids
    assert vault_a != vault_b
    upstream = harness.upstream
    credential_a = next(
        c
        for c in upstream.credentials[vault_a].values()
        if c["auth"].get("mcp_server_url") == "https://a.example/mcp"
    )
    credential_b = next(
        c
        for c in upstream.credentials[vault_b].values()
        if c["auth"].get("mcp_server_url") == "https://b.example/mcp"
    )
    assert (
        upstream.crypto.decrypt(upstream.encrypted_tokens[credential_a["id"]])
        == f"personal-secret-{OWNER_A}".encode()
    )
    assert (
        upstream.crypto.decrypt(upstream.encrypted_tokens[credential_b["id"]])
        == f"personal-secret-{OWNER_B}".encode()
    )
    assert (
        upstream.encrypted_tokens[credential_a["id"]]
        != upstream.encrypted_tokens[credential_b["id"]]
    )
    servers_a = {s.name for s in sessions[a.session_id].agent.mcp_servers}
    servers_b = {s.name for s in sessions[b.session_id].agent.mcp_servers}
    assert "private_a" in servers_a and "private_b" not in servers_a
    assert "private_b" in servers_b and "private_a" not in servers_b
    agent_uuid = derive_agent_uuid(tenant_id=harness.tenant.id, ma_agent_id="ag_daimon")
    for owner, vault in ((OWNER_A, vault_a), (OWNER_B, vault_b)):
        chat = next(
            c for c in upstream.credentials[vault].values() if c["metadata"].get("daimon_chat_jti")
        )
        token = upstream.crypto.decrypt(upstream.encrypted_tokens[chat["id"]]).decode()
        scope = resource_scope(
            tenant_id=str(harness.tenant.id),
            account_id=str(harness.accounts[owner]),
            authorization_id="test-chat",
        )
        verifier = DaimonJWTVerifier(secret=b"t" * 32, sessionmaker=harness.factory)
        access = await verifier.verify_token(token)
        assert access is not None and access.claims["sub"] == scope.account_id
        assert access.claims["chat_agent_id"] == str(agent_uuid)
        assert access.claims["tenant_id"] == str(harness.tenant.id)
        auth = AuthIdentity(
            account_id=uuid.UUID(access.claims["sub"]),
            tenant_id=uuid.UUID(access.claims["tenant_id"]),
            role=Role(access.claims["role"]),
            platform=access.claims["platform"],
            external_id=access.claims["external_id"],
            platform_user_id=access.claims["platform_user_id"],
            chat_agent_id=uuid.UUID(access.claims["chat_agent_id"]),
            token_kind="agent",
            token_jti=uuid.UUID(chat["metadata"]["daimon_chat_jti"]),
        )
        runtime = McpRuntime(
            session_factory=harness.factory,
            client=upstream.client,
            settings=harness.settings,
            deployment_default=DeploymentDefault(),
        )
        mine, theirs = (a, b) if owner == OWNER_A else (b, a)
        own = await _get_routine_impl(runtime, auth, routine_id=mine.routine_id)
        assert own.created_by_user_id == owner and own.last_result_tail == "private answer"
        with pytest.raises(ToolError, match="routine not found"):
            await _get_routine_impl(runtime, auth, routine_id=theirs.routine_id)
        claims = jwt.decode(token, b"t" * 32, algorithms=["HS256"])
        other = OWNER_B if owner == OWNER_A else OWNER_A
        async with harness.factory() as db:
            identity = await get_account_with_tenant(db, account_id=harness.accounts[other])
            registry = await get_mcp_token(db, jti=uuid.UUID(claims["jti"]))
        assert identity is not None and registry is not None
        assert (
            token_row_refusal(registry, claims=claims, identity=identity, now=datetime.now(UTC))
            == "account_mismatch"
        )


async def test_protected_final_action_is_refused_through_the_driver(harness: Harness):
    harness.with_safety(ToolSafetyPolicy(enabled=True))
    harness.upstream.pause_call = ("daimon-mcp", "microsoft_send", {"action": "send_mail"})
    harness.upstream.scripts = [pause_on_public_send]
    reply = await harness.routine()
    (decision,) = harness.upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == "deny"


async def test_endpoint_bound_rule_allows_the_unattended_pause(harness: Harness):
    from daimon.core.authz import Subject
    from daimon.core.stores.agent_tool_rules import set_rule

    harness.with_safety(ToolSafetyPolicy(enabled=True, agent_rules=True))
    async with harness.factory.begin() as db:
        await set_rule(
            db,
            tenant_id=harness.tenant.id,
            agent_id="ag_daimon",
            agent_name="daimon",
            tool_key="slack/send_message",
            server_url=SLACK_SERVER["url"],
            rule="allow",
            scope="chat_and_routines",
            set_by=OWNER_A,
            actor=Subject(is_admin=True),
        )
    harness.upstream.scripts = [pause_on_public_send]
    reply = await harness.routine()
    (decision,) = harness.upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == "allow"


async def test_trusted_hint_is_read_over_the_real_catalogue_path(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    harness.with_safety(ToolSafetyPolicy(enabled=True, trusted_annotations=("slack",)))
    harness.upstream.pause_call = ("slack", "memory_flag_list", {})
    calls = []

    def catalogue(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SLACK_SERVER["url"]
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = (
            {
                "tools": [
                    {
                        "name": "memory_flag_list",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
            if body["method"] == "tools/list"
            else {}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real_init = httpx.AsyncClient.__init__

    def external_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("transport", httpx.MockTransport(catalogue))
        kwargs["trust_env"] = False
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", external_init)
    harness.upstream.scripts = [pause_on_public_send]
    reply = await harness.routine()
    (decision,) = harness.upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == "allow"
    assert calls == ["initialize", "notifications/initialized", "tools/list"]


async def test_mount_outage_matches_live_dm_without_another_owners_memory(harness: Harness):
    harness.upstream.memory_outage = True
    await harness.live_dm("hello without memory", "outage-m1")
    live = await harness.live_session_id()
    reply = await harness.routine()
    assert reply.text == "private answer"
    assert harness.memory_stores(live) == harness.memory_stores(reply.session_id) == {}
    assert harness.manifest(live) == harness.manifest(reply.session_id)


async def test_terminal_success_then_collector_failure_keeps_completed(harness: Harness):
    harness.upstream.file_listing_error = True
    row = await _routine_row(harness)
    await harness.fire(row)
    (intent,) = await harness.intents(row)
    assert intent.state == "completed" and intent.stop_attempts == 0
    async with harness.factory() as db:
        after = await get_routine(db, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.last_error and after.delivery_payload is None


async def test_another_tenants_active_dm_is_untouched(harness: Harness):
    async with harness.factory.begin() as db:
        tenant = await make_tenant(db, workspace_id=str(GUILD + 1))
        await make_tenant_config(db, tenant=tenant, agent_name="daimon", environment_name="default")
        await make_ledger_entry(db, tenant=tenant, delta_usd=Decimal("100"))
        await set_dm_enabled(db, tenant_id=tenant.id, enabled=True)
        await set_dm_agent_name(db, tenant_id=tenant.id, agent_name="daimon")
    other_upstream = Upstream(
        ma_agent(id="ag_other", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_other", name="default", tenant_id=tenant.id),
    )
    other = Harness(harness.factory, tenant, other_upstream)
    try:
        await other.live_dm("other tenant private", "other-m1")
        before = await other.conversation()
        sid = await other.live_session_id()
        result = await harness.routine()
        assert await other.conversation() == before
        assert await other.live_session_id() == sid
        assert set(other.memory_stores(sid)).isdisjoint(harness.memory_stores(result.session_id))
        assert not other_upstream.events(sid, "user.interrupt")
    finally:
        await other.dispatcher.close()
        await other_upstream.client.close()


@pytest.mark.parametrize("change", ["revoke", "retarget"])
@pytest.mark.parametrize("stage", ["chunk", "file"])
async def test_owner_and_destination_are_rechecked_during_real_delivery(
    harness: Harness, change: str, stage: str
):
    from daimon.core.stores.routines import update_routine

    async def generated(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
        upstream.state.write_output(sid, "private.txt", b"personal output")
        async for data in answer("private answer " * (600 if stage == "chunk" else 1))(
            upstream, sid
        ):
            yield data

    harness.upstream.scripts = [generated]
    row = await _routine_row(harness)
    await harness.fire(row)
    sent, files = [], []

    async def changed() -> None:
        if change == "revoke":
            harness.members[OWNER_A] = False
        else:
            async with harness.factory.begin() as db:
                await update_routine(
                    db,
                    row.id,
                    tenant_id=row.tenant_id,
                    destination_kind="channel",
                    destination_id=BUDGET,
                    channel_id=BUDGET,
                )

    async def open_dm(guild: int, owner: int) -> Any:
        class Destination:
            async def send(
                self,
                *,
                content: str | None = None,
                file: Any = None,
                allowed_mentions: object = None,
                **kwargs: Any,
            ) -> None:
                if file is not None:
                    files.append(file)
                else:
                    sent.append(content)
                    if stage == "chunk":
                        await changed()

        return Destination()

    async def no_channel(channel: int) -> Any:
        raise AssertionError("private output cannot be retargeted to a public channel")

    if stage == "file":
        harness.upstream.download_hook = changed
    poster = make_discord_routine_poster(
        harness.factory,
        fetch_channel=no_channel,
        open_dm=open_dm,
        dm_policy=lambda _: DirectMessagePolicy(),
        membership_resolver=harness.resolver,
        output_client=harness.upstream.client,
    )
    await poll_deliveries_once(
        harness.factory, platform="discord", post=poster, now=datetime.now(UTC)
    )
    assert len(sent) == 1
    assert not files, (
        "revocation/retarget during text or file delivery prevents later private output"
    )
    assert (await harness.intents(row))[0].state == "completed"


async def test_unknown_receipt_paginates_and_resolves_every_scoped_match(harness: Harness):
    row = await _routine_row(harness)
    upstream = harness.upstream
    upstream.create_pause_at = 1
    upstream.discovery_error = True
    task = asyncio.create_task(harness.fire(row))
    await asyncio.wait_for(upstream.create_entered.wait(), 30)
    (original,) = upstream.state.sessions.values()
    duplicate = original.model_copy(update={"id": "session_duplicate"})
    upstream.state.sessions[duplicate.id] = duplicate
    foreign = []
    for index in range(105):
        sid = f"session_foreign_{index:03}"
        session = original.model_copy(
            update={
                "id": sid,
                "status": "running",
                "metadata": {
                    **original.metadata,
                    "daimon_account": str(harness.accounts[OWNER_B]),
                },
            }
        )
        upstream.state.sessions[sid] = session
        foreign.append(sid)
    await harness.dispatcher.close()
    await asyncio.gather(task, return_exceptions=True)
    upstream.discovery_error = False
    await harness.boot_recover(row)
    (intent,) = await harness.intents(row)
    assert intent.state == "stopped"
    assert set(intent.discovered_session_ids) == {original.id, duplicate.id}
    assert not any(upstream.events(sid, "user.interrupt") for sid in foreign)
    assert all(upstream.state.sessions[sid].status == "running" for sid in foreign)
