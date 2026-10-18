"""Mocked permission tests for #1097 through the real Teams message ingress.

These complement the core session tests; no live Teams external caller has been run,
so they do not replace the live gap in the security release ask.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.config import McpSettings
from daimon.core.credential_requests import mint_request_token
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores import credential_requests, mcp_oauth_flows
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_sessions import list_live_thread_sessions
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    json_body,
    list_response,
    make_fake_memory_store_handler,
)
from daimon.testing.ma_models import (
    DEFAULT_ENV_NAME,
    ma_agent,
    ma_environment,
    ma_session,
    ma_session_agent,
)
from daimon.testing.turn_router import AGENT_ID, ENV_ID, build_turn_router
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    ADMIN_TEAM,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    build_teams_runtime,
    finish_turns,
    make_channel_activity,
    post_activity,
    running_service,
    settle,
    teams_settings,
)
from .personas import EXTERNAL_ID, GUEST_ID, MEMBER_ID, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
AGENT_UUID = derive_agent_uuid(tenant_id=TENANT, ma_agent_id=AGENT_ID)
ADMIN = AAD_OBJECT_ID
MEMBER = MEMBER_ID
GUEST = GUEST_ID
EXTERNAL = EXTERNAL_ID
FOREIGN_TENANT = "99999999-9999-4999-8999-999999999999"
DOCS_URL = "https://mcp.example.com/docs"
OWN_URL = "https://mcp.example.com/own"
BOT_URL = "https://mcp.example.com/mcp"
DEFAULT = DeploymentDefault(agent_name="test-agent", environment_name=DEFAULT_ENV_NAME)


@dataclasses.dataclass
class _Edge:
    """Capture only the outbound Managed Agents HTTP edge in mocked permission tests."""

    creates: list[dict[str, Any]] = dataclasses.field(default_factory=lambda: [])
    vault_writes: list[dict[str, Any]] = dataclasses.field(default_factory=lambda: [])
    uploads: list[bytes] = dataclasses.field(default_factory=lambda: [])
    sessions: dict[str, dict[str, Any]] = dataclasses.field(default_factory=lambda: {})
    updates: list[tuple[str, dict[str, Any]]] = dataclasses.field(default_factory=lambda: [])
    streams: list[str] = dataclasses.field(default_factory=lambda: [])


def _router(edge: _Edge) -> MARouter:
    """Script MA resolution, sessions, files, vaults, and turn streams at HTTP."""
    router = MARouter()
    toolset: dict[str, Any] = {
        "type": "mcp_toolset",
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }
    agent = ma_agent(
        id=AGENT_ID,
        tenant_id=TENANT,
        system=apply_credential_guidance(""),
        mcp_servers=[
            {"name": "docs", "type": "url", "url": DOCS_URL},
            {"name": "own", "type": "url", "url": OWN_URL},
            {"name": "daimon-mcp", "type": "url", "url": BOT_URL},
        ],
        tools=[{**toolset, "mcp_server_name": name} for name in ("docs", "own", "daimon-mcp")],
    )
    env = ma_environment(id=ENV_ID, tenant_id=TENANT)
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent.model_dump(mode="json")]))
    router.add(
        "GET",
        rf"/v1/agents/{AGENT_ID}",
        lambda _r, _m: httpx.Response(200, json=agent.model_dump(mode="json")),
    )
    router.add(
        "GET", r"/v1/environments", lambda _r, _m: list_response([env.model_dump(mode="json")])
    )
    router.add(
        "GET",
        rf"/v1/environments/{ENV_ID}",
        lambda _r, _m: httpx.Response(200, json=env.model_dump(mode="json")),
    )
    build_turn_router(str(TENANT), router=router, fresh_event_ids=True, stream_hits=edge.streams)
    memory = make_fake_memory_store_handler()
    for method in ("GET", "POST"):
        router.add(method, r"/v1/memory_stores(/.*)?", lambda r, _m: memory(r))

    def create(request: httpx.Request, _match: object) -> httpx.Response:
        body = json_body(request)
        edge.creates.append(body)
        spec = body["agent"]
        override: dict[str, Any] = cast("dict[str, Any]", spec) if isinstance(spec, dict) else {}
        visible: list[dict[str, Any]] = override.get(
            "mcp_servers", [item.model_dump(mode="json") for item in agent.mcp_servers]
        )
        tools: list[dict[str, Any]] = override.get(
            "tools", [item.model_dump(mode="json") for item in agent.tools]
        )
        frozen = ma_session_agent(
            id=AGENT_ID,
            name=agent.name,
            model=agent.model,
            system=agent.system,
            mcp_servers=visible,
            tools=tools,
        )
        identifier = f"sesn_external_qa_{len(edge.creates)}"
        now = datetime.now(UTC).isoformat()
        resources: list[dict[str, Any]] = []
        for resource in cast("list[dict[str, Any]]", body.get("resources") or []):
            if resource["type"] == "file":
                resources.append(
                    resource
                    | {
                        "id": f"res_external_qa_{len(edge.creates)}",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            else:
                resources.append(resource)
        session = ma_session(
            id=identifier,
            agent=frozen,
            environment_id=ENV_ID,
            resources=resources,
            vault_ids=body.get("vault_ids") or [],
            metadata=body.get("metadata") or {},
        ).model_dump(mode="json")
        edge.sessions[identifier] = session
        return httpx.Response(200, json=session)

    router.add("POST", r"/v1/sessions", create)
    router.add(
        "GET",
        r"/v1/sessions/(?P<id>[^/]+)",
        lambda _r, m: httpx.Response(200, json=edge.sessions[m["id"]]),
    )

    def update(request: httpx.Request, match: Any) -> httpx.Response:
        edge.updates.append((match["id"], json_body(request)))
        return httpx.Response(200, json=edge.sessions[match["id"]])

    router.add("POST", r"/v1/sessions/(?P<id>[^/]+)", update)
    router.add(
        "POST",
        r"/v1/sessions/(?P<id>[^/]+)/archive",
        lambda _r, m: httpx.Response(200, json=edge.sessions[m["id"]]),
    )
    router.add(
        "GET",
        r"/v1/files",
        lambda _r, _m: httpx.Response(200, json={"data": [], "has_more": False}),
    )

    def upload(request: httpx.Request, _match: object) -> httpx.Response:
        edge.uploads.append(request.content)
        return httpx.Response(
            200,
            json={
                "id": f"file_external_qa_{len(edge.uploads)}",
                "type": "file",
                "filename": ".env",
                "mime_type": "text/plain",
                "size_bytes": len(request.content),
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

    router.add("POST", r"/v1/files", upload)
    router.add(
        "GET",
        r"/v1/vaults",
        lambda _r, _m: httpx.Response(200, json={"data": [], "has_more": False}),
    )

    def vault(request: httpx.Request, _match: object) -> httpx.Response:
        body = json_body(request)
        identifier = f"vlt_external_qa_{len(edge.creates) + 1}"
        return httpx.Response(
            200,
            json={
                "id": identifier,
                "type": "vault",
                "display_name": body["display_name"],
                "metadata": None,
                "archived_at": None,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

    router.add("POST", r"/v1/vaults", vault)
    router.add(
        "GET",
        r"/v1/vaults/(?P<id>[^/]+)/credentials",
        lambda _r, _m: httpx.Response(200, json={"data": [], "has_more": False}),
    )

    def credential(request: httpx.Request, match: Any) -> httpx.Response:
        body = json_body(request)
        edge.vault_writes.append(body)
        return httpx.Response(
            200,
            json={
                "id": f"vcrd_external_qa_{len(edge.vault_writes)}",
                "type": "credential",
                "vault_id": match["id"],
                "auth": body["auth"],
            },
        )

    router.add("POST", r"/v1/vaults/(?P<id>[^/]+)/credentials", credential)
    return router


async def _sign_in(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    requester: str,
) -> None:
    """Record a completed caller OAuth flow for mocked permission tests."""
    now = datetime.now(UTC)
    request = await credential_requests.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=TENANT,
        agent_id=AGENT_UUID,
        account_id=account_id,
        target="own",
        mcp_server_url=OWN_URL,
        requester_platform_user_id=requester,
        channel_id=CHANNEL_ID,
        expires_at=now + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id=AGENT_ID,
        target_name="test-agent",
        requested_work=None,
    )
    flow = await mcp_oauth_flows.create_flow(
        session,
        state="st_" + uuid.uuid4().hex,
        request_token=request.token,
        tenant_id=TENANT,
        account_id=account_id,
        agent_id=AGENT_UUID,
        server_name="own",
        mcp_server_url=OWN_URL,
        redirect_uri="https://example.test/oauth/callback",
        code_verifier="verifier",
        expires_at=now + timedelta(minutes=10),
    )
    await mcp_oauth_flows.consume_flow(session, state=flow.state, now=now)
    await mcp_oauth_flows.mark_flow_completed(session, state=flow.state, now=now)


async def _seed(factory: async_sessionmaker[AsyncSession], key: str) -> None:
    """Provision the tenant and agent credentials for mocked permission tests."""
    await provision_tenant(
        factory, platform="teams", workspace_id=ENTRA_TENANT_ID, signup_credit=Decimal("100")
    )
    async with factory.begin() as db:
        connected = await get_or_create_platform_principal(
            db, tenant_id=TENANT, platform="teams", external_id="synthetic-connected"
        )
        await _sign_in(db, account_id=connected.account_id, requester="synthetic-connected")
        await set_access_policy(
            db,
            tenant_id=TENANT,
            policy=TenantAccessPolicy.model_validate(
                {
                    "sealed_channel_ids": [CHANNEL_ID],
                    "isolated_channel_ids": [CHANNEL_ID],
                    "agent_channel_pins": {"test-agent": [CHANNEL_ID]},
                    "member_guest_ids": [],
                }
            ),
        )
        await set_fields(
            db,
            scope=ChannelScopeRef(tenant_id=TENANT, channel_id=CHANNEL_ID),
            tenant_id=TENANT,
            agent_name="test-agent",
        )
        await put_agent_file(
            db,
            tenant_id=TENANT,
            agent_id=AGENT_UUID,
            key="API_KEY",
            content="secret",
            set_by_account_id=None,
        )
    await save_agent_mcp_credential(
        sessionmaker=factory,
        fernet=build_multifernet((key,)),
        tenant_id=TENANT,
        agent_id=AGENT_UUID,
        mcp_server_url=DOCS_URL,
        plaintext_token="tok_org",
    )


def _graph(request: httpx.Request) -> httpx.Response:
    """Serve Graph's admin team owner read for mocked permission tests."""
    if request.url.path == f"/v1.0/teams/{ADMIN_TEAM}/members":
        return httpx.Response(
            200,
            json={
                "value": [
                    {"userId": ADMIN, "roles": ["owner"]},
                    {"userId": GUEST, "roles": ["owner", "guest"]},
                ]
            },
        )
    return httpx.Response(404)


@pytest.mark.parametrize("persona", ["admin", "member", "guest", "external"])
async def test_session_credentials_follow_caller_classification(
    db_session_factory: async_sessionmaker[AsyncSession],
    persona: str,
) -> None:
    """Mocked permission tests: ingress chooses credential exposure by persona."""
    key = Fernet.generate_key().decode()
    await _seed(db_session_factory, key)
    teams_api_fake = PersonaTeamsApiFake()
    edge = _Edge()
    client = build_fake_anthropic(_router(edge).dispatch)
    settings = teams_settings().model_copy(update={"admin_team_id": ADMIN_TEAM})
    assert settings.restrict_guests and settings.restrict_external_participants
    runtime = build_teams_runtime(
        db_session_factory,
        anthropic=client,
        teams=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_graph)),
        deployment_default=DEFAULT,
    )
    mcp = McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(BOT_URL))
    runtime.settings.mcp = mcp
    runtime.settings.crypto.keys = (SecretStr(key),)
    runtime = dataclasses.replace(
        runtime,
        turn_deps=dataclasses.replace(
            runtime.turn_deps,
            mcp=mcp,
            fernet=build_multifernet((key,)),
        ),
    )
    user = {"admin": ADMIN, "member": MEMBER, "guest": GUEST, "external": EXTERNAL}[persona]
    activity = make_channel_activity(aad_object_id=user)
    if persona == "external":
        activity["from"] = {"id": f"29:{user}", "aadObjectId": user, "tenantId": FOREIGN_TENANT}
    async with running_service(runtime, teams_api_fake) as service:
        await post_activity(service, activity)
        await finish_turns(service.turns)

    assert len(edge.creates) == 1, f"{persona} must reach session creation"
    body = edge.creates[0]
    resources = [
        resource
        for resource in cast("list[dict[str, Any]]", body.get("resources") or [])
        if resource["type"] == "file"
    ]
    visible: object = body["agent"]
    visible_mapping = cast("dict[str, Any]", visible) if isinstance(visible, dict) else None
    names: set[str] = (
        {
            server["name"]
            for server in cast("list[dict[str, str]]", visible_mapping.get("mcp_servers", []))
        }
        if visible_mapping is not None
        else {"docs", "own", "daimon-mcp"}
    )
    if persona in ("guest", "external"):
        assert resources == [] and edge.uploads == []
        assert not any(
            write["auth"].get("mcp_server_url") == DOCS_URL for write in edge.vault_writes
        )
        assert "docs" not in names
        assert names == {"daimon-mcp"}, "another person's OAuth server is hidden too"
    else:
        assert len(resources) == 1 and resources[0]["mount_path"] == ".env"
        assert len(edge.uploads) == 1
        assert "docs" in names
        assert any(write["auth"].get("mcp_server_url") == DOCS_URL for write in edge.vault_writes)
    async with db_session_factory() as db:
        rows = await list_live_thread_sessions(
            db, tenant_id=TENANT, platform="teams", thread_id=THREAD_ID
        )
        assert rows[0].account_id is not None
        account = await get_account(db, rows[0].account_id)
    assert len(rows) == 1
    assert account is not None
    assert account.role is (Role.ADMIN if persona == "admin" else Role.USER)
    assert rows[0].is_external is (persona in ("guest", "external"))
    assert rows[0].effective_config is not None
    assert rows[0].effective_config.is_external is (persona in ("guest", "external"))

    if persona in ("guest", "external"):
        # A caller's completed OAuth sign-in is stored by the same flow as the core test.
        async with db_session_factory.begin() as db:
            await _sign_in(db, account_id=account.id, requester=user)
        later = make_channel_activity(
            aad_object_id=user,
            conversation_id=f"{CHANNEL_ID};messageid=1700000000002",
            activity_id="activity-own-sign-in",
        )
        if persona == "external":
            later["from"] = {
                "id": f"29:{EXTERNAL}",
                "aadObjectId": EXTERNAL,
                "tenantId": FOREIGN_TENANT,
            }
        async with running_service(runtime, teams_api_fake) as service:
            await post_activity(service, later)
            await finish_turns(service.turns)
        assert len(edge.creates) == 2
        signed_in: object = edge.creates[1]["agent"]
        assert isinstance(signed_in, dict)
        signed_in_servers = cast("list[dict[str, str]]", signed_in["mcp_servers"])
        assert {server["name"] for server in signed_in_servers} == {"own", "daimon-mcp"}


async def test_member_external_member_in_one_thread_keeps_work_separate(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mocked permission tests: an external turn cannot adopt a member's session work."""
    key = Fernet.generate_key().decode()
    await _seed(db_session_factory, key)
    edge = _Edge()
    fake = PersonaTeamsApiFake()
    settings = teams_settings().model_copy(update={"admin_team_id": ADMIN_TEAM})
    client = build_fake_anthropic(_router(edge).dispatch)
    runtime = build_teams_runtime(
        db_session_factory,
        anthropic=client,
        teams=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_graph)),
        deployment_default=DEFAULT,
    )
    mcp = McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(BOT_URL))
    runtime.settings.mcp = mcp
    runtime = dataclasses.replace(
        runtime,
        turn_deps=dataclasses.replace(
            runtime.turn_deps,
            mcp=mcp,
            fernet=build_multifernet((key,)),
        ),
    )
    activities = [
        make_channel_activity(aad_object_id=MEMBER, activity_id="activity-member-1"),
        make_channel_activity(aad_object_id=EXTERNAL, activity_id="activity-external"),
        make_channel_activity(aad_object_id=MEMBER, activity_id="activity-member-2"),
    ]
    activities[1]["from"] = {
        "id": f"29:{EXTERNAL}",
        "aadObjectId": EXTERNAL,
        "tenantId": FOREIGN_TENANT,
    }
    async with running_service(runtime, fake) as service:
        for number, activity in enumerate(activities, start=1):
            await post_activity(service, activity)
            async with asyncio.timeout(10):
                while len(edge.streams) < number or service.turns.in_flight:
                    await asyncio.sleep(0.05)
            await settle(service.turns)
        await finish_turns(service.turns)
    assert len(edge.creates) == 2
    assert edge.streams == ["sesn_external_qa_1", "sesn_external_qa_2", "sesn_external_qa_1"]
    assert edge.creates[0].get("resources") != edge.creates[1].get("resources")
    assert any(r["type"] == "file" for r in edge.sessions["sesn_external_qa_1"]["resources"])
    assert not any(r["type"] == "file" for r in edge.sessions["sesn_external_qa_2"]["resources"])
    assert all(session_id == "sesn_external_qa_1" for session_id, _ in edge.updates), (
        "no member configuration was updated into the external session"
    )
    async with db_session_factory() as db:
        rows = await list_live_thread_sessions(
            db, tenant_id=TENANT, platform="teams", thread_id=THREAD_ID
        )
    assert len(rows) == 2
    assert {row.ma_session_id for row in rows} == {"sesn_external_qa_1", "sesn_external_qa_2"}
    external_row = next(row for row in rows if row.is_external)
    assert external_row.predecessor_id is None and external_row.transfer_file_id is None
