"""Root-bound worker derivation, with no provider requests on refused specs."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, FleetFacts, Place, Subject, Surface, authorize
from daimon.core.neutral_resources import (
    WorkerLimits,
    WorkerPermissions,
    WorkerSpecRefused,
    audit_worker_action,
    resolve_worker_spec,
)
from mux.contracts.config import BackendConfig
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import (
    ChannelRef,
    ModelRef,
    ResourceRef,
    Revision,
    Scope,
    SkillRef,
    ThreadRef,
)
from mux.contracts.resources import (
    Agent,
    AgentPatch,
    AgentSpec,
    EnvironmentSpec,
    MCPConnection,
    NetworkPolicy,
    ResourceBinding,
    ToolSpec,
)
from mux.profiles import backend_selection, get_profile
from pydantic import ValidationError

NOW = datetime(2026, 10, 12, 18, tzinfo=UTC)
POLICY = TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini"))


def parent(provider="anthropic"):
    selection = backend_selection(provider)
    model = ModelRef(provider=provider, id=selection.model or "claude-haiku-5-5")
    scope = Scope(
        tenant_id=str(uuid4()),
        account_id=str(uuid4()),
        principal_id="caller",
        authorization_id="admitted-root",
    )
    root = ThreadRef(
        channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="channel"),
        thread_id="thread",
    )
    fleet = FleetFacts(
        scope=scope,
        root=root,
        origin_id=uuid4(),
        account_id=UUID(scope.account_id),
        run_id=uuid4(),
        enabled=True,
        active=True,
    )
    profile = get_profile(selection.profile)
    tools = (
        ToolSpec(name="helper", kind="custom", input_schema={"type": "object", "properties": {}}),
    )
    if profile.implicit_execution:
        tools += (ToolSpec(name=profile.execution_tool, kind="builtin"),)
    spec = AgentSpec(
        name="root", model=model, system="Protected root instruction", tools=tools, skills=()
    )
    return WorkerPermissions(
        fleet=fleet,
        subject=Subject(platform_user_id="user"),
        agent=AgentRef.of("root"),
        spec=spec,
        base_system="Protected root instruction",
        backend_grants=(selection,),
        models=(model,),
        limits=WorkerLimits(
            budget_usd=Decimal("1"), deadline=NOW + timedelta(minutes=10), max_turns=4
        ),
        environment=EnvironmentSpec(
            name="root-box",
            network=NetworkPolicy(mode="limited", allowed_hosts=("allowed.invalid",)),
        ),
    )


def resolve(root, spec=None, **kwargs):
    return resolve_worker_spec(
        root, spec or root.spec, policy=kwargs.pop("policy", POLICY), now=NOW, **kwargs
    )


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
def test_root_bound_worker_on_each_declared_profile(provider):
    root = parent(provider)
    result = resolve(root)
    assert result.permissions.fleet.depth == 1
    assert result.permissions.fleet.scope.tenant_id == root.fleet.scope.tenant_id
    assert result.permissions.fleet.scope.account_id == root.fleet.scope.account_id
    assert result.permissions.fleet.scope.authorization_id != root.fleet.scope.authorization_id
    assert result.permissions.subject.is_admin is False
    assert result.backend == root.backend_grants[0]
    assert result.spec == root.spec
    assert result.digest == resolve(root).digest
    with pytest.raises(WorkerSpecRefused, match="worker_depth_exceeded"):
        resolve(result.permissions)


@pytest.mark.parametrize(
    "surface", tuple(surface for surface in Surface if surface is not Surface.FLEET)
)
def test_spawn_only_uses_the_fleet_surface(surface):
    root = parent()
    assert not authorize(
        POLICY,
        subject=root.subject,
        action=Action.SPAWN_WORKER,
        surface=surface,
        agent=root.agent,
        place=Place(channel_id="thread", parent_channel_id="channel"),
        fleet=root.fleet,
    )


@pytest.mark.parametrize(
    "updates",
    (
        {"enabled": False},
        {"enabled": 1},
        {"active": False},
        {"depth": 1},
        {"depth": True},
        {"scope": Scope.platform(reason="not delegated")},
        {"account_id": uuid4()},
    ),
)
def test_disabled_worker_forged_account_and_platform_scope_fail_closed(updates):
    root = parent()
    root = replace(root, fleet=replace(root.fleet, **updates))
    with pytest.raises(WorkerSpecRefused):
        resolve(root)


@pytest.mark.parametrize(
    "policy",
    (
        TenantAccessPolicy(channel_rules={"channel": ChannelRule(writers="none")}),
        TenantAccessPolicy(agent_rules={"root": AgentRule(runs_in=("elsewhere",))}),
        TenantAccessPolicy(invoker_user_ids=("someone-else",)),
    ),
)
def test_current_invoker_writer_and_agent_rules_bind_spawning(policy):
    with pytest.raises(WorkerSpecRefused):
        resolve(parent(), policy=policy)


def test_admin_never_gets_private_surface_agent_exemption():
    root = replace(parent(), subject=Subject(is_admin=True, platform_user_id="admin"))
    with pytest.raises(WorkerSpecRefused, match="runs_elsewhere"):
        resolve(
            root, policy=TenantAccessPolicy(agent_rules={"root": AgentRule(runs_in=("elsewhere",))})
        )


@pytest.mark.parametrize(
    "field",
    ("tool", "schema", "permission", "skill", "mcp", "metadata", "effort", "extensions", "model"),
)
def test_individual_permission_escalations_refuse(field):
    root = parent()
    original = ToolSpec(
        name="helper", kind="custom", input_schema={"type": "object"}, permission="ask"
    )
    root = replace(root, spec=root.spec.model_copy(update={"tools": (original,)}))
    updates = {
        "tool": {"tools": (ToolSpec(name="new", kind="builtin"),)},
        "schema": {"tools": (original.model_copy(update={"input_schema": {"type": "string"}}),)},
        "permission": {"tools": (original.model_copy(update={"permission": "auto"}),)},
        "skill": {"skills": (SkillRef(id="foreign", version="1"),)},
        "mcp": {"mcp_servers": (MCPConnection(name="unbound", url="https://other.invalid"),)},
        "metadata": {"metadata": {"daimon_reader_source": "another-agent"}},
        "effort": {"model": root.spec.model.model_copy(update={"options": {"effort": "high"}})},
        "extensions": {
            "extensions": (ExtensionConfig(namespace="anthropic.agent_tools", version=1, value={}),)
        },
        "model": {"model": root.spec.model.model_copy(update={"id": "ungranted"})},
    }[field]
    with pytest.raises(WorkerSpecRefused):
        resolve(root, root.spec.model_copy(update=updates))


@pytest.mark.parametrize("kind", ("budget", "deadline", "max_turns"))
def test_positive_limits_cannot_be_extended(kind):
    root = parent()
    changes = {
        "budget": {"budget_usd": Decimal("1.01")},
        "deadline": {"deadline": root.limits.deadline + timedelta(seconds=1)},
        "max_turns": {"max_turns": 5},
    }[kind]
    with pytest.raises(WorkerSpecRefused, match=kind):
        resolve(root, limits=WorkerLimits.model_validate(root.limits.model_dump() | changes))


@pytest.mark.parametrize("value", (True, 0, -1))
def test_turn_counter_is_strict_and_positive(value):
    with pytest.raises(ValidationError):
        WorkerLimits(budget_usd=Decimal("1"), deadline=NOW, max_turns=value)


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), True, 1.1))
def test_budget_requires_finite_decimal(value):
    with pytest.raises(ValidationError):
        WorkerLimits(budget_usd=value, deadline=NOW, max_turns=1)


def test_expired_deadline_and_missing_timezone_refuse_before_remaining_floor():
    root = parent()
    with pytest.raises(WorkerSpecRefused, match="deadline"):
        resolve(root, limits=root.limits.model_copy(update={"deadline": NOW}))
    with pytest.raises(ValidationError):
        WorkerLimits(budget_usd=Decimal("1"), deadline=NOW.replace(tzinfo=None), max_turns=1)


def test_backend_allowlist_and_model_grants_are_independent():
    root = parent("openai")
    with pytest.raises(WorkerSpecRefused, match="backend"):
        resolve(root, policy=TenantAccessPolicy())
    with pytest.raises(WorkerSpecRefused, match="backend"):
        resolve(
            root,
            backend=BackendConfig(
                backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
            ),
        )


def test_hosted_implicit_shell_is_not_removed_by_tools_empty():
    root = parent("openai")
    with pytest.raises(WorkerSpecRefused, match="tools"):
        resolve(replace(root, spec=root.spec.model_copy(update={"tools": ()})))
    with pytest.raises(WorkerSpecRefused, match="sandbox"):
        resolve(replace(root, environment=None))


def test_environment_network_never_widens():
    root = parent()
    with pytest.raises(WorkerSpecRefused, match="sandbox"):
        resolve(
            root,
            environment=root.environment.model_copy(
                update={"network": NetworkPolicy(mode="unrestricted")}
            ),
        )
    result = resolve(
        root,
        environment=root.environment.model_copy(update={"network": NetworkPolicy(mode="none")}),
    )
    assert result.environment.network.mode == "none"


def test_resource_identity_and_native_mount_authority_are_not_fabricated():
    root = parent()
    ref = ResourceRef(
        id="file",
        kind="artifact",
        provider=root.spec.model.provider,
        account_scope_id="workspace",
        tenant_id=root.fleet.scope.tenant_id,
        account_id=root.fleet.scope.account_id,
    )
    resource = ResourceBinding(id="input", kind="artifact", resource=ref, target_path="input.txt")
    root = replace(root, resources=(resource,))
    assert resolve(root, resources=(resource,)).resources == (resource,)
    with pytest.raises(WorkerSpecRefused, match="files"):
        resolve(root, resources=(resource.model_copy(update={"target_path": "../escape"}),))
    foreign = resource.model_copy(
        update={"resource": ref.model_copy(update={"tenant_id": str(uuid4())})}
    )
    with pytest.raises(WorkerSpecRefused, match="scope"):
        resolve(replace(root, resources=(foreign,)), resources=(foreign,))


@pytest.mark.parametrize(
    "url",
    (
        "https://user:pass@server.invalid",
        "https://server.invalid?token=hidden",
        "https://server.invalid#secret",
        "https://[invalid",
        "http://server.invalid",
    ),
)
def test_even_matching_parent_url_cannot_delegate_hidden_credentials(url):
    root = parent()
    server = MCPConnection(
        name="server",
        url=url,
        tool_policy={"allowed_tools": ["read"], "require_approval": "always"},
    )
    root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (server,)}))
    with pytest.raises(WorkerSpecRefused) as caught:
        resolve(root)
    assert url not in str(caught.value)


def test_mcp_policy_narrows_but_does_not_remove_approval():
    root = parent()
    server = MCPConnection(
        name="server",
        url="https://server.invalid",
        credential_ref="bound-ref",
        tool_policy={"allowed_tools": ["read", "write"], "require_approval": "always"},
    )
    root = replace(root, spec=root.spec.model_copy(update={"mcp_servers": (server,)}))
    narrowed = server.model_copy(
        update={"tool_policy": {"allowed_tools": ["read"], "require_approval": "always"}}
    )
    assert resolve(
        root, root.spec.model_copy(update={"mcp_servers": (narrowed,)})
    ).spec.mcp_servers == (narrowed,)
    for policy in (
        {"allowed_tools": ["read"], "require_approval": "never"},
        {
            "allowed_tools": ["read"],
            "require_approval": "always",
            "headers": {"authorization": "hidden"},
        },
    ):
        with pytest.raises(WorkerSpecRefused):
            resolve(
                root,
                root.spec.model_copy(
                    update={"mcp_servers": (server.model_copy(update={"tool_policy": policy}),)}
                ),
            )


def test_clone_requires_exact_revision_and_sparse_override_does_not_mutate_source():
    root = parent()
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider=root.spec.model.provider,
        account_scope_id="workspace",
        tenant_id=root.fleet.scope.tenant_id,
        account_id=root.fleet.scope.account_id,
    )
    source = Agent(ref=ref, revision=Revision(local=1), spec=root.spec, created_at=NOW)
    root = replace(root, source_agents=(ref,), source_revisions=((ref, source.revision),))
    result = resolve(root, source, overrides=AgentPatch(tools=(), system="Worker role"))
    assert result.spec.tools == ()
    assert result.spec.system == root.base_system + "\n\nWorker role"
    assert source.spec == root.spec
    with pytest.raises(WorkerSpecRefused, match="stale"):
        resolve(root, source.model_copy(update={"revision": Revision(local=2)}))
    with pytest.raises(WorkerSpecRefused):
        resolve(root, source, overrides=AgentPatch(model=None))


def test_current_worker_rule_cannot_be_hidden_by_root_rule():
    root = parent()
    with pytest.raises(WorkerSpecRefused, match="runs_elsewhere"):
        resolve(
            root,
            root.spec.model_copy(update={"name": "blocked"}),
            policy=TenantAccessPolicy(agent_rules={"blocked": AgentRule(runs_in=())}),
        )


def test_nested_schema_and_input_are_not_shared_with_result():
    root = parent()
    result = resolve(root)
    assert result.spec is not root.spec
    assert result.spec.tools[0].input_schema is not root.spec.tools[0].input_schema


async def test_actual_anthropic_clone_refuses_opaque_authority_without_extra_io():
    from daimon.testing.ma_models import ma_agent
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
    from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
    from mux.drivers.anthropic.resources.agents import AnthropicAgents

    root = parent()
    scope = root.fleet.scope
    native = ma_agent(
        id="source",
        name="root",
        tenant_id=scope.tenant_id,
        model=root.spec.model.id,
        system="Source role",
    )
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "GET",
            "/v1/agents/source",
            httpx.Response(200, json=native.model_dump(mode="json", exclude_unset=True)),
        )
    )
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider="anthropic",
        account_scope_id="workspace",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    async with trace.client() as sdk:
        source = await AnthropicAgents(
            sdk, "workspace", ResourceAuthorization(scope, frozenset({("agent", "source")}))
        ).retrieve(scope, ref)
        root = replace(
            root,
            spec=root.spec.model_copy(update={"metadata": source.spec.metadata}),
            source_agents=(ref,),
            source_revisions=((ref, source.revision),),
        )
        with pytest.raises(WorkerSpecRefused, match="effort|extensions"):
            resolve(root, source)
        # Explicitly remove the opaque native knobs rather than guessing their
        # authority. The independent parent model remains the only model grant.
        result = resolve(
            root, source, overrides=AgentPatch(extensions=None, model=root.spec.model, tools=())
        )
        assert result.spec.system == root.base_system + "\n\nSource role"
        assert result.spec.extensions is None
        assert source.native == native.model_dump(mode="json", exclude_unset=True)
    trace.assert_consumed()
    assert [(r.method, r.path, r.body) for r in trace.requests] == [
        ("GET", "/v1/agents/source", b"")
    ]


async def test_actual_openai_clone_preserves_implicit_execution_and_refuses_added_tool():
    from daimon.testing.isolated_sdk import build_async_openai
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
    from mux.drivers.openai._common import Context
    from mux.drivers.openai.agents import OpenAIAgents
    from mux.drivers.openai.transport import SDKTransport

    root = parent("openai")
    scope = root.fleet.scope
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "GET",
            "/v1/agents/source",
            httpx.Response(
                200,
                json={
                    "id": "source",
                    "created_at": 0,
                    "name": "root",
                    "model": root.spec.model.id,
                    "instructions": "Source role",
                    "tools": [],
                    "metadata": {"mux_tenant": scope.tenant_id},
                },
            ),
        )
    )
    ref = ResourceRef(
        id="source",
        kind="agent",
        provider="openai",
        account_scope_id="project",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    async with build_async_openai(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(trace.dispatch),
    ) as sdk:
        source = await OpenAIAgents(
            Context(
                SDKTransport(sdk),
                "project",
                root.backend_grants[0].profile,
                lambda requested, kind, id_: (
                    requested == scope and kind == "agent" and id_ == "source"
                ),
            )
        ).retrieve(scope, ref)
        root = replace(
            root,
            spec=root.spec.model_copy(update={"metadata": source.spec.metadata}),
            source_agents=(ref,),
            source_revisions=((ref, source.revision),),
        )
        result = resolve(root, source)
        assert result.spec.tools == (ToolSpec(name="bash", kind="builtin"),)
        with pytest.raises(WorkerSpecRefused, match="tools"):
            resolve(
                root, source, overrides=AgentPatch(tools=(ToolSpec(name="new", kind="custom"),))
            )
    trace.assert_consumed()
    assert len(trace.requests) == 1


async def test_fleet_audit_uses_real_root_identity_and_existing_tenant_store(db_session):
    from daimon.core.stores.security_audit import list_events
    from daimon.testing.factories import make_account, make_tenant

    root = parent()
    tenant = await make_tenant(db_session, id=UUID(root.fleet.scope.tenant_id))
    await make_account(db_session, id=root.fleet.account_id, tenant=tenant)
    root = replace(root, agent_id=uuid4())
    worker_id = uuid4()
    row = await audit_worker_action(
        db_session,
        root,
        worker_id=worker_id,
        operation="spawn",
        outcome="allowed",
        reason="authorized",
    )
    rows = await list_events(db_session, tenant_id=tenant.id)
    assert rows == [row]
    assert row.agent_id == root.agent_id
    assert row.account_id == root.fleet.account_id
    assert row.scope == f"fleet:{root.fleet.run_id}:worker:{worker_id}"
    assert row.turn_ref == f"origin:{root.fleet.origin_id}"
    assert row.operation == "spawn" and row.tool_name == "fleet"
    assert root.base_system not in row.model_dump_json()
    assert await list_events(db_session, tenant_id=uuid4()) == []
    with pytest.raises(WorkerSpecRefused):
        await audit_worker_action(
            db_session,
            replace(root, fleet=replace(root.fleet, account_id=uuid4())),
            worker_id=worker_id,
            operation="spawn",
            outcome="allowed",
            reason="authorized",
        )
    assert len(await list_events(db_session, tenant_id=tenant.id)) == 1
