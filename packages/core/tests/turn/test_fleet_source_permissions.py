"""Admin ceilings meet actual captured source through the resource owner."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.authz import FleetFacts, Place, Subject, Surface, build_agent_ref
from daimon.core.channel_backend import load_backend_agent
from daimon.core.mux_compat import retrieve_agent
from daimon.core.neutral_resources import (
    AgentFleetGrant,
    WorkerLimits,
    WorkerSpecRefused,
    fleet_root_permissions,
    resolve_worker_spec,
)
from daimon.core.sessions import project_session_resources
from daimon.core.stores import agent_memory_stores
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.turn.admission import AdmissionGrant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import MCPConnection, ResourceBinding, ToolSpec
from mux.profiles import backend_selection

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned

NOW = datetime(2026, 10, 13, 23, tzinfo=UTC)


@pytest.fixture
async def captured(owned, request):
    service, identity, actor, sessions = owned
    await enable(service, identity, actor)
    scope = Scope(
        tenant_id=str(identity.tenant_id),
        account_id=str(actor.account_id),
        principal_id="admitted-caller",
        authorization_id="admitted-root",
    )
    live = getattr(request, "param", None) in ("haiku55", "haiku55_credentials")
    capture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/anthropic_live_root.json").read_text()
    )
    native = ma_agent(
        id="root-source",
        name="helper",
        tenant_id=identity.tenant_id,
        model="claude-haiku-5-5",
        system="Protected source instruction",
        version=10 if live else 1,
        tools=capture["tools"]
        if live
        else [
            {
                "type": "custom",
                "name": "helper",
                "description": "Read facts",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        skills=capture["skills"] if live else (),
        mcp_servers=capture["mcp_servers"]
        if live
        else [{"type": "url", "name": "private", "url": "https://private.invalid"}]
        if getattr(request, "param", None) == "mcp"
        else (),
    )
    if live:
        native = native.model_copy(update={"execution_identity": capture["execution_identity"]})
    environment = ma_environment(tenant_id=identity.tenant_id)
    if live:
        from anthropic.types.beta import BetaEnvironment

        environment = BetaEnvironment.model_validate(
            {
                **environment.model_dump(mode="json"),
                **capture["environment"],
            }
        )
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "GET",
            "/v1/agents/root-source",
            httpx.Response(200, json=native.model_dump(mode="json", exclude_unset=True)),
        )
    )
    async with trace.client() as client:
        actual = await retrieve_agent(client, native.id, scope=scope)
        source = await load_backend_agent(
            client,
            sessions,
            tenant_id=identity.tenant_id,
            agent_name="helper",
            actual_agent=actual,
        )
        async with sessions.begin() as db:
            await agent_memory_stores.insert_memory_store(
                db,
                tenant_id=identity.tenant_id,
                agent_id=source.agent_id,
                memory_store_id="memstore-captured",
            )
        from daimon.core.config import McpSettings
        from pydantic import SecretStr

        source = await project_session_resources(
            source,
            agent=actual,
            environment=environment,
            session_factory=sessions,
            tenant_id=identity.tenant_id,
            agent_uuid=source.agent_id,
            account_id=actor.account_id,
            resource_scope=scope,
            memory_read_only=True,
            mcp_settings=McpSettings(
                public_url="https://mcp.invalid/mcp", jwt_secret=SecretStr("offline-placeholder")
            )
            if getattr(request, "param", None) in ("caller_credentials", "haiku55_credentials")
            else None,
        )
    trace.assert_consumed()
    assert [(r.method, r.path, r.body) for r in trace.requests] == [
        ("GET", "/v1/agents/root-source", b""),
    ]
    value = AgentFleetGrant(
        max_workers=4,
        choices=(
            (backend_selection("anthropic"), ModelRef(provider="anthropic", id="claude-haiku-5-5")),
            (backend_selection("openai"), ModelRef(provider="openai", id="gpt-6-luna")),
            (
                backend_selection("gemini").model_copy(update={"model": "gemini-3.5-flash-lite"}),
                ModelRef(provider="gemini", id="gemini-3.5-flash-lite"),
            ),
        ),
        limits=WorkerLimits(
            budget_usd=Decimal("0.9"), deadline=NOW + timedelta(hours=1), max_turns=1
        ),
        max_run_seconds=600,
        max_worker_seconds=120,
        max_root_turns=3,
    )
    preview = await service.preview_fleet_grant(source, value, now=NOW)
    await service.set_fleet_grant(
        source,
        value,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=NOW,
    )
    grant = await service.read_fleet_grant(source.tenant_id, source.agent_id, now=NOW)
    async with sessions() as db:
        policy = await load_access_policy(db, tenant_id=source.tenant_id)
    thread = ThreadRef(
        channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="channel"),
        thread_id="thread",
    )
    place = Place(channel_id="thread", parent_channel_id="channel", category_id="category")
    admission = AdmissionGrant(
        tenant_id=source.tenant_id,
        subject=Subject(platform_user_id="member"),
        agent=build_agent_ref(actual.name, actual.metadata or {}),
        surface=Surface.CHANNEL,
        turn_place=place,
        run_place=place,
        channel_id="channel",
        thread_id="thread",
        is_dm=False,
    )
    fleet = FleetFacts(
        scope=scope,
        root=thread,
        origin_id=uuid4(),
        account_id=actor.account_id,
        run_id=uuid4(),
        place=place,
        enabled=True,
        active=True,
    )
    return source, admission, grant, fleet, policy, trace


def compose(inputs, **changes):
    source, admission, grant, fleet, policy, _ = inputs
    arguments = dict(fleet=fleet, policy=policy, now=NOW, seal_ids=frozenset({"root-seal"}))
    arguments.update(changes)
    return fleet_root_permissions(source, admission, grant, **arguments)


async def test_real_source_and_scoped_admin_grant_preserve_exact_authority(captured):
    source, admission, grant, fleet, _, trace = captured
    permissions = compose(captured)
    assert permissions.fleet == fleet and permissions.subject == admission.subject
    assert permissions.agent == admission.agent and permissions.agent_id == source.agent_id
    assert permissions.backend_grants == grant.backends and permissions.models == grant.models
    assert permissions.spec.system == source.effective_spec.system
    assert permissions.spec.tools[0].name == "helper"
    assert permissions.environment == source.environment.spec
    assert permissions.source_revisions == ((source.source.ref, source.source.revision),)
    assert permissions.resources[0].resource.id == source.memory_store_id
    assert permissions.resources[0].resource.account_id == fleet.scope.account_id
    assert permissions.resources[0].native.value["resource"]["access"] == "read_only"
    assert permissions.seal_ids == frozenset({"root-seal"}) and permissions.memory_read_only
    assert permissions.limits.deadline == NOW + timedelta(seconds=120)
    assert len(trace.requests) == 1


@pytest.mark.parametrize("role", (0, 1, 2))
async def test_each_exact_role_pair_derives_without_source_model_substitution(captured, role):
    permissions = compose(captured)
    backend, model = captured[2].choices[role]
    spec = permissions.spec.model_copy(
        update={
            "model": model,
            "tools": (),
            "skills": (),
            "mcp_servers": (),
            "declared_drops": (
                "memory",
                "sandbox",
                "skills",
                "mcp",
                "tools",
                "files",
                "repository",
            ),
        }
    )
    from mux.contracts.config import BackendConfig

    child = resolve_worker_spec(
        permissions,
        spec,
        policy=captured[4],
        now=NOW,
        backend=BackendConfig.model_validate(backend.model_dump()),
        limits=permissions.limits,
        captured_source=captured[0],
    )
    assert child.backend == backend and child.spec.model == model
    assert not child.permissions.subject.is_admin
    assert child.permissions.backend_grants == (backend,) and child.permissions.models == (model,)


@pytest.mark.parametrize("captured", ("caller_credentials", "mcp"), indirect=True)
async def test_actual_projector_keeps_unsupported_main_credentials_and_mcp(captured):
    source, _, _, _, _, trace = captured
    assert source.resource_requirements or source.effective_spec.mcp_servers
    with pytest.raises(WorkerSpecRefused):
        compose(captured)
    assert len(trace.requests) == 1


async def test_finite_grant_durations_remain_under_the_existing_turn_ceiling(captured):
    from daimon.core.turn.ceiling import TURN_CEILING_S

    source, admission, grant, fleet, policy, _ = captured
    grant = grant.model_copy(update={"max_run_seconds": 10**100, "max_worker_seconds": 10**100})
    permissions = fleet_root_permissions(
        source, admission, grant, fleet=fleet, policy=policy, now=NOW
    )
    assert permissions.limits.deadline == NOW + timedelta(seconds=TURN_CEILING_S)


@pytest.mark.parametrize(
    "fault",
    (
        "credential",
        "mcp",
        "opaque",
        "repository",
        "unknown_extension",
        "native_tools",
        "missing_memory",
        "foreign_scope",
        "foreign_environment",
        "archived_environment",
        "expired",
        "pending_epoch",
        "invalid_grant",
        "naive_clock",
        "policy",
        "category",
        "subject",
    ),
)
async def test_unsupported_missing_or_foreign_capture_refuses_before_native_io(captured, fault):
    source, admission, grant, fleet, policy, trace = captured
    if fault in ("credential", "opaque"):
        source = source.model_copy(
            update={
                "resource_requirements": (
                    ResourceBinding(
                        id="retained", kind="credential" if fault == "credential" else "native"
                    ),
                )
            }
        )
    elif fault == "mcp":
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={
                        "mcp_servers": (
                            MCPConnection(name="private", url="https://private.invalid"),
                        ),
                    }
                )
            }
        )
    elif fault == "repository":
        from mux.contracts.resources import WorkspaceSource

        source = source.model_copy(
            update={
                "repository_sources": (
                    WorkspaceSource(
                        kind="repository", target_path=".", repository_url="https://repo.invalid"
                    ),
                )
            }
        )
    elif fault in ("unknown_extension", "native_tools"):
        extension = ExtensionConfig(namespace="anthropic.unrepresented_intent", version=1, value={})
        if fault == "native_tools":
            # A per-tool name that fails the closed builtin-tool schema: still a
            # schema failure, distinct from a well-formed toolset with no neutral
            # form (e.g. a bare `agent_toolset_20260401` or an `mcp_toolset`),
            # which is excluded from the ceiling rather than refused.
            extension = ExtensionConfig(
                namespace="anthropic.agent_tools",
                version=1,
                value={
                    "tools": [
                        {
                            "type": "agent_toolset_20260401",
                            "configs": [{"name": "nonexistent_tool", "enabled": True}],
                        }
                    ],
                },
            )
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={
                        "extensions": source.effective_spec.extensions + (extension,),
                    }
                )
            }
        )
    elif fault == "missing_memory":
        source = source.model_copy(update={"memory_store_id": None})
    elif fault == "foreign_scope":
        source = source.model_copy(
            update={"resource_scope": fleet.scope.model_copy(update={"account_id": str(uuid4())})}
        )
    elif fault in ("foreign_environment", "archived_environment"):
        environment = source.environment
        environment = (
            environment.model_copy(
                update={"ref": environment.ref.model_copy(update={"tenant_id": str(uuid4())})}
            )
            if fault == "foreign_environment"
            else environment.model_copy(update={"archived_at": NOW})
        )
        source = source.model_copy(update={"environment": environment})
    elif fault == "expired":
        grant = grant.model_copy(
            update={"limits": grant.limits.model_copy(update={"deadline": NOW})}
        )
    elif fault == "pending_epoch":
        grant = grant.model_copy(update={"epoch": 0})
    elif fault == "invalid_grant":
        grant = grant.model_copy(update={"max_workers": True})
    elif fault == "policy":
        policy = TenantAccessPolicy(channel_rules={"category": ChannelRule(writers="none")})
    elif fault == "category":
        admission = replace(
            admission, run_place=replace(admission.run_place, category_id="neighbour")
        )
    elif fault == "subject":
        admission = replace(admission, tenant_id=uuid4())
    with pytest.raises(WorkerSpecRefused):
        fleet_root_permissions(
            source,
            admission,
            grant,
            fleet=fleet,
            policy=policy,
            now=NOW.replace(tzinfo=None) if fault == "naive_clock" else NOW,
        )
    assert len(trace.requests) == 1


_HAIKU55_BUILTINS = ("bash", "read", "edit", "grep", "glob", "write", "web_fetch", "web_search")


@pytest.mark.parametrize("captured", ("haiku55", "haiku55_credentials"), indirect=True)
async def test_fleet_root_ceiling_keeps_builtins_and_excludes_the_daimon_mcp_toolset(captured):
    """Live readback structure, with synthetic skill IDs and no credentials."""
    permissions = compose(captured)
    assert {tool.name for tool in permissions.spec.tools} == set(_HAIKU55_BUILTINS)
    assert all(tool.kind == "builtin" for tool in permissions.spec.tools)
    assert not any(tool.kind == "mcp_toolset" for tool in permissions.spec.tools)
    assert not permissions.spec.mcp_servers
    assert not any(r.kind == "credential" for r in permissions.resources)
    assert len(permissions.spec.skills) == 11
    assert len(captured[5].requests) == 1


@pytest.mark.parametrize("captured", ("haiku55", "haiku55_credentials"), indirect=True)
async def test_worker_cannot_inherit_the_excluded_daimon_mcp_toolset(captured):
    """A worker that asks for the excluded mcp toolset is refused by the
    existing ceiling-subset attenuation — the exclusion in fleet_root_permissions
    is enough; no worker can ever request what never entered the ceiling."""
    permissions = compose(captured)
    requested = permissions.spec.model_copy(
        update={"tools": (ToolSpec(name="daimon-mcp", kind="mcp_toolset"),)}
    )
    with pytest.raises(WorkerSpecRefused) as refused:
        resolve_worker_spec(
            permissions,
            requested,
            policy=captured[4],
            now=NOW,
            limits=permissions.limits,
            captured_source=captured[0],
        )
    assert refused.value.field == "tools" and refused.value.reason == "not_subset"


async def test_fleet_schema_error_refuses_before_freeze(captured, monkeypatch):
    from daimon.core import neutral_resources

    source = captured[0]
    invalid = ExtensionConfig(
        namespace="anthropic.agent_tools", version=1, value={"tools": [{"type": "unknown"}]}
    )
    source = source.model_copy(
        update={
            "effective_spec": source.effective_spec.model_copy(update={"extensions": (invalid,)})
        }
    )

    def freeze(*args, **kwargs):
        pytest.fail("schema-invalid source reached freeze")

    monkeypatch.setattr(neutral_resources, "freeze_root_permissions", freeze)
    with pytest.raises(WorkerSpecRefused, match="tools"):
        fleet_root_permissions(
            source, captured[1], captured[2], fleet=captured[3], policy=captured[4], now=NOW
        )


@pytest.mark.parametrize("captured", ("haiku55", "haiku55_credentials"), indirect=True)
async def test_native_clone_inherits_only_the_projected_ceiling(captured):
    from mux.drivers.anthropic.resources.agents import agent_payload

    permissions = compose(captured)
    child = resolve_worker_spec(
        permissions,
        captured[0].source,
        policy=captured[4],
        now=NOW,
        limits=permissions.limits,
        resources=permissions.resources,
        environment=permissions.environment,
        captured_source=captured[0],
    )
    assert child.spec.tools == permissions.spec.tools
    assert not child.spec.mcp_servers and not child.spec.extensions
    payload = agent_payload(child.spec)
    assert all(t["default_config"]["enabled"] is False for t in payload["tools"])
    assert {c["name"] for t in payload["tools"] for c in t["configs"]} == set(_HAIKU55_BUILTINS)


@pytest.mark.parametrize("captured", ("haiku55", "haiku55_credentials"), indirect=True)
async def test_live_manifest_worker_session_build_uses_only_neutral_grants(captured, owned):
    from daimon.core.config import McpSettings
    from daimon.core.mux_compat import sdk_agent
    from daimon.core.sessions import create_session
    from daimon.testing.ma import MARouter
    from daimon.testing.ma_models import ma_session
    from mux.drivers.anthropic.resources.agents import agent_payload
    from pydantic import SecretStr

    source, _, _, _, policy, _ = captured
    permissions = compose(captured)
    worker = resolve_worker_spec(
        permissions,
        permissions.spec,
        policy=policy,
        now=NOW,
        resources=permissions.resources,
        environment=permissions.environment,
        limits=permissions.limits,
        captured_source=source,
    )
    source = source.model_copy(update={"resource_scope": worker.permissions.fleet.scope})
    execution = source.source.model_copy(
        update={
            "ref": source.source.ref.model_copy(update={"id": "worker-agent"}),
            "native": None,
            "spec": worker.spec,
        }
    )
    executing = sdk_agent(execution)
    from anthropic.types.beta import BetaEnvironment

    environment = BetaEnvironment.model_validate(dict(source.environment.native))
    router = MARouter()
    snapshots = {}

    def native(call, match):
        if call.method == "POST":
            payload = json.loads(call.content)
            session = ma_session(
                id="worker-session",
                agent=executing,
                environment_id=environment.id,
                metadata=payload["metadata"],
                resources=payload["resources"],
            )
            snapshots["session"] = session.model_dump(mode="json")
        return httpx.Response(200, json=snapshots["session"])

    router.add("POST", r"/v1/sessions", native)
    router.add("GET", r"/v1/sessions/worker-session", native)
    router.add(
        "GET",
        r"/v1/environments/.*",
        lambda call, match: httpx.Response(200, json=dict(source.environment.native)),
    )
    trace = ScriptedTransport(router=router)

    async def fence():
        return None

    settings = (
        McpSettings(
            public_url="https://mcp.invalid/mcp", jwt_secret=SecretStr("offline-placeholder")
        )
        if source.resource_requirements
        else None
    )
    async with trace.client() as client:
        from daimon.core.scope import ResolvedConfig
        from daimon.core.turn.admission import Admission
        from daimon.core.turn.prepare import DEFAULT_MA_CAPABILITIES, ProviderPreparationRequest
        from daimon.core.turn.runtime_manifest import prepare_source
        from daimon.testing.turn_fakes import deps_for
        from mux.contracts.config import ConfigRevision

        deps = replace(
            deps_for(client, owned[3]),
            agent_backends=True,
            channel_backends=True,
            mcp=settings or McpSettings(),
        )
        request = ProviderPreparationRequest(
            deps=deps,
            admission=Admission(
                account_id=permissions.fleet.account_id,
                agent=sdk_agent(source.source),
                environment=environment,
                config=ResolvedConfig(agent_name="helper", environment_name="test-env"),
                memory_read_only=True,
                backend_revision=ConfigRevision.create(
                    permissions.fleet.root.channel, 1, worker.backend
                ),
                backend_revision_origin="agent",
            ),
            scope=worker.permissions.fleet.scope,
            tenant_id=source.tenant_id,
            platform="discord",
            external_user_id="member",
            thread_id="worker-thread",
            session_account_id=permissions.fleet.account_id,
            reuse_existing=True,
            capabilities=DEFAULT_MA_CAPABILITIES,
            transfer=None,
            deadline=worker.limits.deadline,
            now=lambda: NOW,
            worker=worker,
            source=source,
        )
        prepared = await prepare_source(request)
        assert prepared.target_spec == worker.spec
        result = await create_session(
            client,
            agent=executing,
            environment=environment,
            tenant_id=source.tenant_id,
            account_id=permissions.fleet.account_id,
            agent_uuid=source.agent_id,
            session_factory=owned[3],
            resource_source=source,
            worker=worker,
            before_create=fence,
            memory_read_only=True,
            mcp_settings=settings,
        )
    assert not result.agent.mcp_servers
    assert len(result.agent.skills) == 11
    assert len(result.resources) == 1
    assert len(executing.tools) == 1
    assert agent_payload(worker.spec)["tools"][0]["default_config"]["enabled"] is False
    assert not any("vault" in call.path for call in trace.requests)
    trace.assert_consumed()


@pytest.mark.parametrize("captured", ("haiku55",), indirect=True)
async def test_same_provider_native_loss_is_declared_unless_projecting_a_worker_ceiling(captured):
    from daimon.core.turn.runtime_manifest import classify_source_obligations

    permissions = compose(captured)
    missing = classify_source_obligations(
        captured[0],
        permissions.spec,
        environment=permissions.environment,
        resources=permissions.resources,
    )
    assert {"tools", "mcp"} <= missing
    assert not classify_source_obligations(
        captured[0],
        permissions.spec,
        environment=permissions.environment,
        resources=permissions.resources,
        worker_ceiling=True,
    )


async def test_excluded_toolset_removes_its_credential_ref_only(captured):
    from daimon.core.turn.runtime_manifest import worker_source_ceiling

    source = captured[0]
    native = ExtensionConfig(
        namespace="anthropic.agent_tools",
        version=1,
        value={"tools": [{"type": "mcp_toolset", "mcp_server_name": "private"}]},
    )
    source = source.model_copy(
        update={
            "effective_spec": source.effective_spec.model_copy(
                update={
                    "extensions": (native,),
                    "mcp_servers": (
                        MCPConnection(
                            name="private",
                            url="https://private.invalid",
                            credential_ref="private-key",
                        ),
                    ),
                }
            ),
            "resource_requirements": (
                ResourceBinding(
                    id="native-credential", kind="credential", credential_ref="private-key"
                ),
                ResourceBinding(
                    id="unrelated-credential", kind="credential", credential_ref="other-key"
                ),
            ),
        }
    )
    ceiling = worker_source_ceiling(source)
    assert not ceiling.effective_spec.mcp_servers
    assert ceiling.resource_requirements == (source.resource_requirements[1],)
