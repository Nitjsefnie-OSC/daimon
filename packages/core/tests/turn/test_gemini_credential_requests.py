"""Actual native SDK/catalog/origin and Discord serialization proofs for A3."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

import discord
import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import gemini_credential_requests as native_mcp
from daimon.adapters.mcp.tools.discord._posted_card import build_card_view
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.github_connect_cards import ConnectCard, discord_embed_payload
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.posted_controls import PostedCard
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.neutral_catalog import CatalogConflict
from daimon.core.stores.turn_origins import create_origin, delete_origin
from daimon.core.turn.gemini_credential_requests import (
    CONTROL_PREFIX,
    GeminiNativeCredentialAdapter,
    NativeCredentialOrigin,
    NativeCredentialRequest,
    NativeOfferReceipt,
)
from daimon.testing.db import build_test_engine, require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.isolated_sdk import build_gemini_client
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from mux.contracts.config import ConfigRevision, ResolvedBackend
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, ProviderBinding, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
from mux.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.drivers.gemini.transport import Object
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from pydantic import TypeAdapter
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from google.genai import Client


TLS_NAMES = frozenset(
    {
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSLKEYLOGFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)


def replay_client(transport: httpx.MockTransport) -> Client:
    if any(
        name.upper() in TLS_NAMES
        or name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        for name in os.environ
    ):
        raise ValueError("A3 replay refuses ambient SDK configuration")
    return build_gemini_client(transport=transport)


NOW = datetime(2026, 10, 11, tzinfo=UTC)


_OBJECT: TypeAdapter[Object] = TypeAdapter(Object)


class NativeWire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.agents: dict[str, Object] = {}
        self.get_changes: Object = {}

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.method == "POST" and req.url.path == "/v1beta/agents":
            body = _OBJECT.validate_json(req.content, strict=True)
            id_ = body["id"]
            if not isinstance(id_, str):
                raise ValueError("missing native identity")
            self.agents[id_] = body
            return httpx.Response(200, json={"id": id_, "tools": None})
        if req.method == "GET" and req.url.path.startswith("/v1beta/agents/"):
            return httpx.Response(
                200, json={**self.agents[req.url.path.rsplit("/", 1)[-1]], **self.get_changes}
            )
        raise AssertionError("No other native operation belongs in an A3 offer")


class NativeDelivery:
    """Owned durable file registry, real shared card renderer and Discord wire."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.surface = DiscordSurfaceHarness(
            evidence_id="native-credential",
            session_id="host-session-scoped",
            root_turn_id="root-offer",
            clock=lambda: 0.0,
            model_id="gemini-3.8-flash",
        )
        self.registered: list[NativeCredentialRequest] = []
        self.on_register: Callable[[], Awaitable[None]] | None = None
        self.bad_receipt = False

    async def register(self, request: NativeCredentialRequest) -> None:
        # Independent registry receipt; neither legacy request store is touched.
        (self.path / request.ref.id).write_bytes(
            TypeAdapter(NativeCredentialRequest).dump_json(request)
        )
        self.registered.append(request)
        if self.on_register is not None:
            await self.on_register()

    async def post(
        self, request: NativeCredentialRequest, card: ConnectCard | PostedCard
    ) -> NativeOfferReceipt:
        stored = TypeAdapter(NativeCredentialRequest).validate_json(
            (self.path / request.ref.id).read_bytes()
        )
        assert stored == request and request in self.registered
        if isinstance(card, ConnectCard):
            view = discord.ui.View(timeout=None)
            view.add_item(
                discord.ui.Button(
                    label=card.title,
                    custom_id=request.custom_id,
                    style=discord.ButtonStyle.primary,
                )
            )
            message = await self.surface.thread.send(
                embed=discord.Embed.from_dict(discord_embed_payload(card)),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            message = await self.surface.thread.send(
                view=build_card_view(card),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return NativeOfferReceipt(
            request.ref,
            request.thread.model_copy(update={"thread_id": "foreign"})
            if self.bad_receipt
            else request.thread,
            str(message.id),
        )


@dataclass
class Case:
    adapter: GeminiNativeCredentialAdapter
    origin: NativeCredentialOrigin
    wire: NativeWire
    delivery: NativeDelivery
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture
def owned_engine(request: pytest.FixtureRequest) -> AsyncEngine:
    # Guard all binds before asking the DB fixture to allocate/connect.
    dsn = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if dsn is None:
        pytest.skip("native A3 requires an explicitly owned or isolated-CI database")
    schema = cast(str, request.getfixturevalue("db_schema"))
    guard = build_test_engine(dsn, schema)
    try:
        require_owned_sessionmaker(async_sessionmaker(guard))
    except ValueError:
        pytest.skip("native A3 requires an owned or isolated-CI database")
    finally:
        guard.sync_engine.dispose()
    engine = cast(AsyncEngine, request.getfixturevalue("db_engine"))
    require_owned_sessionmaker(async_sessionmaker(engine))
    return engine


@pytest.fixture
def owned_clean(request: pytest.FixtureRequest, owned_engine: AsyncEngine) -> None:
    # Resolve cleanup only after the owned bind guard. A direct db_clean
    # dependency would allocate its session-scoped engine ahead of this guard.
    request.getfixturevalue("db_clean")


@pytest.fixture
async def case(owned_engine: AsyncEngine, owned_clean: None, tmp_path: Path) -> AsyncIterator[Case]:
    sessions = async_sessionmaker(owned_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        tenant = await make_tenant(db, workspace_id="100")
        actor = await make_account(db, tenant=tenant)
        resource = await make_account(db, tenant=tenant)
        await make_platform_principal(
            db, tenant=tenant, account=actor, platform="discord", external_id="103"
        )
        await set_role(db, actor.id, Role.ADMIN)
        place = await create_origin(
            db,
            tenant_id=tenant.id,
            account_id=actor.id,
            platform="discord",
            parent_channel_id="101",
            thread_id="102",
            responder_ma_agent_id="not-a-native-authority",
            responder_name="Legacy field never used",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN,
            now=NOW,
            expires_at=NOW + timedelta(hours=2),
        )
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(resource.id),
        principal_id=f"mcp:{actor.id}",
        authorization_id="native-execution",
    )
    source = AgentSpec(
        name="daimon",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        tools=(ToolSpec(name="code_execution", kind="builtin"),),
        skills=(),
        metadata={MA_METADATA_KEY_NAME: "daimon"},
        system="Help.",
    )
    wire = NativeWire()
    sdk = replay_client(httpx.MockTransport(wire.handle))
    interactions = FakeTransport()
    try:
        driver = GeminiManagedAgents(
            interactions,
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id="gemini-workspace",
            resource_catalog_enabled=True,
            resource_catalog_transport=SDKAgentCatalogTransport(sdk),
        )
        host = NeutralResourceHost(
            driver,
            sessions,
            scope=scope,
            account_scope_id="gemini-workspace",
            operation_key="credential-target",
            resource_catalog_enabled=True,
            catalog_adapter=driver.catalog_adapter,
        )
        # Actual authored native target POST, independent SDK GET and catalog CAS.
        # This is a component proof, not the still-unbound channel-copy route.
        target = source.model_copy(
            update={
                "name": "credential-team",
                "metadata": {MA_METADATA_KEY_NAME: "credential-team"},
            }
        )
        row = await host.publish("credential-team", target, expected_revision=0, key="target:1")
        thread = ThreadRef(
            channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="101"),
            thread_id="102",
        )
        binding = ProviderBinding(
            id="owned-native-binding",
            provider="gemini",
            profile="gemini.inline_reuse",
            generation=1,
            config_revision=1,
            thread=thread,
            legacy_account_id=str(actor.id),
            native_refs={
                "agent": row.agent.ref.id,
                "session": "host-session-scoped",
                "account_scope": "gemini-workspace",
            },
        )
        store = PostgresStateStore(sessions)
        await store.put_config_revision(
            ConfigRevision.create(
                thread.channel,
                1,
                ResolvedBackend(
                    backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
                ),
            )
        )
        await store.put_binding(binding, expected_generation=0)
        origin = NativeCredentialOrigin(
            place.id,
            actor.id,
            binding,
            ResourceRef(
                id="host-session-scoped",
                kind="session",
                provider="gemini",
                account_scope_id="gemini-workspace",
                tenant_id=scope.tenant_id,
                account_id=str(actor.id),
            ),
            row.agent.ref,
            row.catalog_id,
            row.local_revision,
            scope,
        )
        delivery = NativeDelivery(tmp_path)
        adapter = GeminiNativeCredentialAdapter(
            sessions,
            host,
            operator_enabled=True,
            delivery=delivery,
            public_base_url="https://mcp.invalid",
            now=lambda: NOW,
        )
        yield Case(adapter, origin, wire, delivery, sessions)
        assert interactions.requests == []
    finally:
        await sdk.aio.aclose()
        sdk.close()
    # A3 never constructs a legacy client or reads an MA identity authority.


@pytest.mark.parametrize("kind,target", [("github", "GitHub"), ("env", "ACME_API_KEY")])
async def test_actual_native_target_posts_shared_offer(case: Case, kind: str, target: str) -> None:
    before = len(case.wire.requests)
    receipt = await case.adapter.offer(case.origin, kind=kind, target=target)  # type: ignore[arg-type]
    registered = case.delivery.registered[0]
    message = case.delivery.surface.gateway.surviving_messages()[0]
    assert receipt.message_id == message.message_id and receipt.thread == case.origin.binding.thread
    assert receipt.request == registered.ref and receipt.request.provider == "gemini"
    assert (
        registered.origin.agent == case.origin.agent and registered.agent_name == "credential-team"
    )
    assert registered.expires_at == NOW + timedelta(minutes=30)
    assert registered.origin.requester_account_id != uuid.UUID(case.adapter.host.scope.account_id)
    assert all(req.method == "GET" for req in case.wire.requests[before:])
    assert len(case.wire.requests[before:]) == 1
    assert CONTROL_PREFIX in message.payload_json and '"ztc:' not in message.payload_json
    assert "gh_connect:" not in message.payload_json
    if kind == "github":
        assert message.component_labels == ("Connect GitHub",)
        payload = _OBJECT.validate_json(message.payload_json)
        embeds = payload["embeds"]
        assert isinstance(embeds, list) and len(embeds) == 1
        embed = _OBJECT.validate_python(embeds[0])
        assert embed["title"] == "Connect GitHub"
        assert embed["description"] == (
            "Nothing is connected yet.\n\nTap the button and tick the repos credential-team can use."
        )
        assert embed["author"] == {
            "name": "Daimon",
            "icon_url": "https://mcp.invalid/web/daimon-face.png",
        }
        assert embed["thumbnail"] == {"url": "https://mcp.invalid/web/github-mark.png"}
        assert "ask your daimon operator" not in " ".join(message.embed_texts).lower()
    else:
        assert "Enter it privately" in " ".join(message.component_labels)
        assert "ACME_API_KEY" in message.payload_json
    # The routing/offer is observed, not an expiry/OAuth/submit certificate.
    case.adapter.now = lambda: NOW + timedelta(seconds=1920)
    assert case.delivery.surface.gateway.surviving_messages()[0] == message
    assert len(case.delivery.surface.gateway.events) == 1


@pytest.mark.parametrize("missing", ["flag", "delivery"])
async def test_default_off_or_missing_native_delivery_refuses_before_io(
    case: Case, missing: str
) -> None:
    if missing == "flag":
        adapter = GeminiNativeCredentialAdapter(
            case.sessions,
            case.adapter.host,
            delivery=case.delivery,
            public_base_url="https://mcp.invalid",
        )
    else:
        adapter = GeminiNativeCredentialAdapter(
            case.sessions,
            case.adapter.host,
            operator_enabled=True,
            public_base_url="https://mcp.invalid",
        )
    before = len(case.wire.requests)
    with pytest.raises(UnsupportedCapability):
        await adapter.offer(case.origin, kind="github")
    assert len(case.wire.requests) == before and case.delivery.registered == []


@pytest.mark.parametrize(
    "bad",
    [
        "provider",
        "workspace",
        "resource_account",
        "session_account",
        "generation",
        "origin",
        "catalog",
        "binding_workspace",
        "shared_binding",
        "foreign_binding_account",
    ],
)
async def test_foreign_or_stale_target_refuses_before_native_io(case: Case, bad: str) -> None:
    origin = case.origin
    if bad == "provider":
        origin = replace(origin, agent=origin.agent.model_copy(update={"provider": "anthropic"}))
    elif bad == "workspace":
        origin = replace(
            origin, agent=origin.agent.model_copy(update={"account_scope_id": "foreign"})
        )
    elif bad == "resource_account":
        origin = replace(
            origin, agent=origin.agent.model_copy(update={"account_id": str(uuid.uuid4())})
        )
    elif bad == "session_account":
        origin = replace(
            origin, session=origin.session.model_copy(update={"account_id": str(uuid.uuid4())})
        )
    elif bad == "generation":
        origin = replace(origin, binding=origin.binding.model_copy(update={"generation": 2}))
    elif bad == "origin":
        origin = replace(origin, origin_id=uuid.uuid4())
    elif bad == "catalog":
        origin = replace(origin, catalog_revision=2)
    elif bad in ("shared_binding", "foreign_binding_account"):
        origin = replace(
            origin,
            binding=origin.binding.model_copy(
                update={"legacy_account_id": None if bad == "shared_binding" else str(uuid.uuid4())}
            ),
        )
    else:
        origin = replace(
            origin,
            binding=origin.binding.model_copy(
                update={
                    "native_refs": dict(origin.binding.native_refs) | {"account_scope": "foreign"}
                }
            ),
        )
    before = len(case.wire.requests)
    with pytest.raises((ScopeViolation, CatalogConflict)):
        await case.adapter.offer(origin, kind="github")
    assert len(case.wire.requests) == before and case.delivery.registered == []


@pytest.mark.parametrize("changed", ["origin", "binding", "role", "writers", "catalog"])
async def test_fresh_refusal_after_native_registration_posts_nothing(
    case: Case, changed: str
) -> None:
    async def mutate() -> None:
        if changed == "catalog":
            row = await case.adapter.host.entry(case.origin.catalog_id)
            await case.adapter.host.publish(
                row.catalog_id,
                row.agent.spec.model_copy(update={"system": "Changed after registration."}),
                expected_revision=row.local_revision,
                key="target:changed",
            )
        elif changed == "binding":
            await case.adapter.store.put_binding(
                case.origin.binding.model_copy(update={"generation": 2}), expected_generation=1
            )
        else:
            async with case.sessions.begin() as db:
                if changed == "origin":
                    await delete_origin(db, origin_id=case.origin.origin_id)
                elif changed == "role":
                    await set_role(db, case.origin.requester_account_id, Role.USER)
                else:
                    await set_access_policy(
                        db,
                        tenant_id=uuid.UUID(case.adapter.host.scope.tenant_id),
                        policy=TenantAccessPolicy(
                            channel_rules={"101": ChannelRule(writers="none")}
                        ),
                    )

    case.delivery.on_register = mutate
    with pytest.raises((ScopeViolation, CatalogConflict)):
        await case.adapter.offer(case.origin, kind="github")
    assert len(case.delivery.registered) == 1
    assert case.delivery.surface.gateway.events == []


async def test_false_delivery_receipt_is_refused(case: Case) -> None:
    case.delivery.bad_receipt = True
    with pytest.raises(ScopeViolation, match="receipt mismatched"):
        await case.adapter.offer(case.origin, kind="github")
    assert len(case.delivery.surface.gateway.events) == 1


async def test_reserved_env_key_refuses_before_native_readback(case: Case) -> None:
    before = len(case.wire.requests)
    with pytest.raises(ValueError, match="key name"):
        await case.adapter.offer(case.origin, kind="env", target="SSLKEYLOGFILE")
    assert len(case.wire.requests) == before and not case.delivery.registered


@pytest.mark.parametrize("kind", ["github", "env"])
async def test_native_mcp_dispatch_posts_actual_verified_target_offer(
    case: Case, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    server = FastMCP("explicit-native")
    auth = AuthIdentity(
        tenant_id=uuid.UUID(case.adapter.host.scope.tenant_id),
        account_id=case.origin.requester_account_id,
        role=Role.ADMIN,
    )

    async def authenticated(ctx: Context) -> AuthIdentity:
        return auth

    async def granted(identity: AuthIdentity, origin_id: str) -> native_mcp.GeminiCredentialGrant:
        assert identity == auth and origin_id == str(case.origin.origin_id)
        return native_mcp.GeminiCredentialGrant(case.adapter, case.origin)

    monkeypatch.setattr(native_mcp, "_auth", authenticated)
    await native_mcp.register_gemini_credential_tools(server, operator_enabled=True, grants=granted)
    before = len(case.wire.requests)
    arguments = {"origin_context_id": str(case.origin.origin_id)}
    if kind == "env":
        arguments["key"] = "ACME_API_KEY"
    result = await server.call_tool(
        "github_connect" if kind == "github" else "request_agent_key", arguments
    )
    content = cast(Sequence[object], result.content)
    assert isinstance(content[0], TextContent)
    receipt = TypeAdapter(NativeOfferReceipt).validate_json(content[0].text)
    message = case.delivery.surface.gateway.surviving_messages()[0]
    assert receipt.message_id == message.message_id
    assert receipt.request == case.delivery.registered[0].ref
    assert CONTROL_PREFIX in message.payload_json
    assert len(case.wire.requests[before:]) == 1 and case.wire.requests[-1].method == "GET"
    if kind == "github":
        assert message.component_labels == ("Connect GitHub",)
        payload = _OBJECT.validate_json(message.payload_json)
        embeds = payload["embeds"]
        assert isinstance(embeds, list) and len(embeds) == 1
        embed = _OBJECT.validate_python(embeds[0])
        assert embed["title"] == "Connect GitHub"
        assert embed["description"] == (
            "Nothing is connected yet.\n\nTap the button and tick the repos credential-team can use."
        )
        assert embed["author"] == {
            "name": "Daimon",
            "icon_url": "https://mcp.invalid/web/daimon-face.png",
        }
        assert embed["thumbnail"] == {"url": "https://mcp.invalid/web/github-mark.png"}
    else:
        assert "ACME_API_KEY" in message.payload_json


@pytest.mark.parametrize("bad", ["tenant", "requester", "origin", "host_account", "host_tenant"])
async def test_native_mcp_factory_mismatch_refuses_before_io(
    case: Case, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    server = FastMCP("explicit-native")
    auth = AuthIdentity(
        tenant_id=uuid.uuid4() if bad == "tenant" else uuid.UUID(case.adapter.host.scope.tenant_id),
        account_id=uuid.uuid4() if bad == "requester" else case.origin.requester_account_id,
        role=Role.ADMIN,
    )

    async def authenticated(ctx: Context) -> AuthIdentity:
        return auth

    async def granted(identity: AuthIdentity, origin_id: str) -> native_mcp.GeminiCredentialGrant:
        return native_mcp.GeminiCredentialGrant(case.adapter, case.origin)

    if bad in ("host_account", "host_tenant"):
        host = case.adapter.host
        assert isinstance(host.backend, GeminiManagedAgents)
        field = "account_id" if bad == "host_account" else "tenant_id"
        case.adapter.host = NeutralResourceHost(
            host.backend,
            case.sessions,
            scope=host.scope.model_copy(update={field: str(uuid.uuid4())}),
            account_scope_id=host.workspace,
            operation_key="foreign-factory-return",
            resource_catalog_enabled=True,
            catalog_adapter=host.backend.catalog_adapter,
        )
    monkeypatch.setattr(native_mcp, "_auth", authenticated)
    await native_mcp.register_gemini_credential_tools(server, operator_enabled=True, grants=granted)
    before = len(case.wire.requests)
    with pytest.raises(ToolError, match="native credential grant mismatched"):
        await server.call_tool(
            "github_connect",
            {"origin_context_id": str(uuid.uuid4() if bad == "origin" else case.origin.origin_id)},
        )
    assert len(case.wire.requests) == before and not case.delivery.registered
    assert not case.delivery.surface.gateway.events


@pytest.mark.parametrize("axis", ["tenant", "account", "principal", "authorization", "workspace"])
async def test_returned_host_scope_is_verified_before_database_or_native_io(
    case: Case, owned_engine: AsyncEngine, axis: str
) -> None:
    host = case.adapter.host
    assert isinstance(host.backend, GeminiManagedAgents)
    expected = case.origin.resource_scope
    if axis == "workspace":
        workspace = "foreign-workspace"
        # The constructor itself is required to refuse a mismatched workspace.
        before = len(case.wire.requests)
        with pytest.raises(UnsupportedCapability):
            NeutralResourceHost(
                host.backend,
                case.sessions,
                scope=expected,
                account_scope_id=workspace,
                operation_key="foreign-return",
                resource_catalog_enabled=True,
                catalog_adapter=host.backend.catalog_adapter,
            )
        assert len(case.wire.requests) == before
        return
    field = {
        "tenant": "tenant_id",
        "account": "account_id",
        "principal": "principal_id",
        "authorization": "authorization_id",
    }[axis]
    returned = NeutralResourceHost(
        host.backend,
        case.sessions,
        scope=expected.model_copy(update={field: str(uuid.uuid4())}),
        account_scope_id=host.workspace,
        operation_key="foreign-return",
        resource_catalog_enabled=True,
        catalog_adapter=host.backend.catalog_adapter,
    )
    # A legitimately constructed host can still be the wrong factory result.
    case.adapter.host = returned
    sql: list[object] = []

    def observed(*args: object, **kwargs: object) -> None:
        sql.append(None)

    event.listen(owned_engine.sync_engine, "before_cursor_execute", observed)
    before = len(case.wire.requests)
    try:
        with pytest.raises(ScopeViolation, match="outside its grant"):
            await case.adapter.offer(case.origin, kind="github")
    finally:
        event.remove(owned_engine.sync_engine, "before_cursor_execute", observed)
    assert sql == [] and len(case.wire.requests) == before
    assert not case.delivery.registered and not case.delivery.surface.gateway.events


async def test_native_readback_drift_refuses_before_register_or_post(case: Case) -> None:
    case.wire.get_changes = {"tools": []}
    with pytest.raises(ProviderError, match="catalog_native_definition_drift"):
        await case.adapter.offer(case.origin, kind="github")
    assert not case.delivery.registered and not case.delivery.surface.gateway.events


async def test_expired_origin_refuses_before_provider_readback(case: Case) -> None:
    case.adapter.now = lambda: NOW + timedelta(hours=3)
    before = len(case.wire.requests)
    with pytest.raises(ScopeViolation, match="expired"):
        await case.adapter.offer(case.origin, kind="github")
    assert len(case.wire.requests) == before and not case.delivery.registered


class NamesOnly(Mapping[str, str]):
    def __init__(self, name: str) -> None:
        self.name = name

    def __iter__(self) -> Iterator[str]:
        yield self.name

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        raise AssertionError("read ambient value")


@pytest.mark.parametrize(
    "name",
    [
        "GOOGLE_API_KEY",
        "GEMINI_FUTURE_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "SSLKEYLOGFILE",
        "HTTPS_PROXY",
    ],
)
def test_replay_sdk_refuses_present_value_traps_before_allocation(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("allocated SDK/HTTP client")

    wire = httpx.MockTransport(lambda _: httpx.Response(500))
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", NamesOnly(name))
        patch.setattr("google.genai.Client", forbidden)
        patch.setattr(httpx, "Client", forbidden)
        patch.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(ValueError, match="ambient SDK"):
            replay_client(wire)
