"""A fleet worker that runs AS another on-server agent, under the requester's permissions."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from daimon.core.access_policy import AgentRule
from daimon.core.authz import build_agent_ref
from daimon.core.channel_backend import load_backend_agent
from daimon.core.mux_compat import retrieve_agent
from daimon.core.neutral_resources import (
    WorkerSpecRefused,
    agent_worker_permissions,
    resolve_worker_spec,
    worker_agent_allowed,
)
from daimon.core.sessions import project_session_resources
from daimon.core.stores import agent_memory_stores
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

from ..test_agent_backend import owned as owned
from .test_fleet_source_permissions import NOW, compose
from .test_fleet_source_permissions import captured as captured

RESEARCHER_SYSTEM = "You are the research agent. Cite sources."


async def capture_agent(owned, scope, *, ma_id, name, model="claude-haiku-5-5"):
    """Capture another on-server agent the way the root's source is captured."""
    _, identity, actor, sessions = owned
    native = ma_agent(
        id=ma_id,
        name=name,
        tenant_id=identity.tenant_id,
        model=model,
        system=RESEARCHER_SYSTEM,
        version=1,
        tools=[
            {
                "type": "custom",
                "name": "lookup",
                "description": "Look a fact up",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "GET",
            f"/v1/agents/{ma_id}",
            httpx.Response(200, json=native.model_dump(mode="json", exclude_unset=True)),
        )
    )
    async with trace.client() as client:
        actual = await retrieve_agent(client, native.id, scope=scope)
        source = await load_backend_agent(
            client, sessions, tenant_id=identity.tenant_id, agent_name=name, actual_agent=actual
        )
        async with sessions.begin() as db:
            await agent_memory_stores.insert_memory_store(
                db,
                tenant_id=identity.tenant_id,
                agent_id=source.agent_id,
                memory_store_id=f"memstore-{name}",
            )
        source = await project_session_resources(
            source,
            agent=actual,
            environment=ma_environment(tenant_id=identity.tenant_id),
            session_factory=sessions,
            tenant_id=identity.tenant_id,
            agent_uuid=source.agent_id,
            account_id=actor.account_id,
            resource_scope=scope,
            memory_read_only=True,
        )
    return source, actual


def any_agent(grant):
    return grant.model_copy(update={"worker_agents_mode": "any"})


def test_worker_agents_field_has_three_explicit_states():
    root, other = uuid4(), uuid4()

    class Grant:
        pass

    legacy = Grant()
    assert worker_agent_allowed(legacy, root, root)  # the root is always allowed
    assert not worker_agent_allowed(legacy, root, other)  # absent: never silently widened
    anyone = Grant()
    anyone.worker_agents_mode = "any"
    assert worker_agent_allowed(anyone, root, other)
    listed = Grant()
    listed.worker_agents = (other,)
    assert worker_agent_allowed(listed, root, other)
    assert not worker_agent_allowed(listed, root, uuid4())


async def test_a_worker_runs_as_the_named_agent_with_its_own_ceiling(captured, owned):
    source, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    worker_root = agent_worker_permissions(
        root, researcher, as_researcher, any_agent(grant), policy=policy, now=NOW
    )
    # Its own agent, prompt and tools; the root's run, subject and finite limits.
    assert worker_root.agent_id == researcher.agent_id != root.agent_id
    assert worker_root.spec.system == RESEARCHER_SYSTEM
    assert [tool.name for tool in worker_root.spec.tools] == ["lookup"]
    assert worker_root.source_revisions == ((researcher.source.ref, researcher.source.revision),)
    assert (worker_root.fleet, worker_root.subject, worker_root.limits) == (
        root.fleet,
        root.subject,
        root.limits,
    )
    worker = resolve_worker_spec(
        worker_root,
        researcher.source,
        policy=policy,
        now=NOW,
        captured_source=researcher,
        resources=worker_root.resources,
        environment=worker_root.environment,
        limits=worker_root.limits,
    )
    assert worker.spec.name == "researcher"
    assert RESEARCHER_SYSTEM in worker.spec.system and "Protected source" not in worker.spec.system
    assert worker.permissions.agent_id == researcher.agent_id
    assert not worker.permissions.subject.is_admin


async def test_a_legacy_grant_never_lets_workers_run_as_another_agent(captured, owned):
    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    with pytest.raises(WorkerSpecRefused, match="scope: not_subset"):
        agent_worker_permissions(root, researcher, as_researcher, grant, policy=policy, now=NOW)
    listed = grant.model_copy(update={"worker_agents": (researcher.agent_id,)})
    assert agent_worker_permissions(
        root, researcher, as_researcher, listed, policy=policy, now=NOW
    ).agent_id == (researcher.agent_id)


async def test_the_worker_agents_model_must_be_granted(captured, owned):
    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    pricey, actual = await capture_agent(
        owned, fleet.scope, ma_id="pricey-source", name="pricey", model="claude-opus-5-5"
    )
    as_pricey = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    # Unlike the root, a worker agent's own model is never implied by the grant.
    with pytest.raises(WorkerSpecRefused, match="backend"):
        agent_worker_permissions(root, pricey, as_pricey, any_agent(grant), policy=policy, now=NOW)


async def test_the_requester_must_be_able_to_run_that_agent_here(captured, owned):
    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    elsewhere = policy.model_copy(
        update={"agent_rules": {"researcher": AgentRule(runs_in=("another-channel",))}}
    )
    with pytest.raises(WorkerSpecRefused, match="origin"):
        agent_worker_permissions(
            root, researcher, as_researcher, any_agent(grant), policy=elsewhere, now=NOW
        )


async def test_a_worker_agent_admission_must_be_the_roots_requester_and_place(captured, owned):
    from daimon.core.authz import Subject

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    someone_else = replace(
        admission,
        agent=build_agent_ref(actual.name, actual.metadata or {}),
        subject=Subject(platform_user_id="someone-else"),
    )
    with pytest.raises(WorkerSpecRefused, match="origin: unbound"):
        agent_worker_permissions(
            root, researcher, someone_else, any_agent(grant), policy=policy, now=NOW
        )
    assert Decimal(0) < root.limits.budget_usd


def ports_for(worker_root, researcher):
    from unittest.mock import create_autospec

    from daimon.core.fleet_orchestration import FleetPorts

    ports = create_autospec(FleetPorts, instance=True)
    ports.agent_worker_parent.return_value = worker_root
    ports.capture_source.return_value = researcher
    ports.read_clone.return_value = researcher.source
    admitted = []

    class Admitted(Exception):
        pass

    async def admit(parent, resolved, **kwargs):
        admitted.append((parent, resolved))
        raise Admitted

    ports.admit.side_effect = admit
    return ports, admitted, Admitted


async def test_spawn_as_agent_admits_that_agent_with_the_task_after_its_own_prompt(captured, owned):
    from daimon.core.fleet_orchestration import FleetOrchestrator
    from mux.contracts.config import BackendConfig
    from mux.profiles import backend_selection

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    worker_root = agent_worker_permissions(
        root, researcher, as_researcher, any_agent(grant), policy=policy, now=NOW
    )
    ports, admitted, Admitted = ports_for(worker_root, researcher)
    with pytest.raises(Admitted):
        await FleetOrchestrator(ports).spawn(
            root,
            policy,
            now=NOW,
            operation_key="R1",
            role="research",
            backend=BackendConfig.model_validate(backend_selection("anthropic").model_dump()),
            limits=root.limits,
            as_agent="researcher",
            task="Find three sources on SQLite write locking.",
        )
    ports.agent_worker_parent.assert_awaited_once_with(root, "researcher")
    ((parent, resolved),) = admitted
    # Admission runs under the agent's own authority, inside the root's run.
    assert parent is worker_root and parent.fleet == root.fleet
    assert resolved.spec.name == "researcher"
    assert resolved.source_agent == (researcher.source.ref, researcher.source.revision)
    assert resolved.permissions.agent_id == researcher.agent_id
    system = resolved.spec.system
    assert system.index(RESEARCHER_SYSTEM) < system.index("Your task: Find three sources")


async def test_spawn_as_agent_refuses_an_agent_outside_the_roots_run(captured, owned):
    from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    worker_root = agent_worker_permissions(
        root, researcher, as_researcher, any_agent(grant), policy=policy, now=NOW
    )
    # An owner that hands back authority bound to another run, or wider limits, is refused.
    for changed in (
        replace(worker_root, fleet=replace(worker_root.fleet, run_id=uuid4())),
        replace(
            worker_root,
            limits=worker_root.limits.model_copy(update={"budget_usd": Decimal("100")}),
        ),
    ):
        ports, admitted, _ = ports_for(changed, researcher)
        with pytest.raises(FleetRefused, match="worker_agent_unbound"):
            await FleetOrchestrator(ports).agent_worker(root, policy, agent_name="researcher")
        assert not admitted
    ports, _, _ = ports_for(worker_root, researcher)
    with pytest.raises(FleetRefused, match="worker_agent_excludes_spec"):
        await FleetOrchestrator(ports).spawn(
            root,
            policy,
            now=NOW,
            operation_key="R2",
            role="research",
            backend=None,
            limits=root.limits,
            as_agent="researcher",
            agent=root.spec,
        )


@pytest.mark.parametrize("mode", ["any", "legacy"])
async def test_fleet_agents_lists_only_agents_this_run_can_spawn_as(
    captured, owned, monkeypatch, mode
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from daimon.core.defaults import ma_index
    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.turn import runtime_manifest

    _, _, grant, fleet, policy, _ = captured
    root = compose(captured)
    _, identity, _, sessions = owned
    tenant = identity.tenant_id
    agents = [
        ma_agent(id="root-source", name="helper", tenant_id=tenant, model="claude-haiku-5-5"),
        ma_agent(
            id="researcher-source", name="researcher", tenant_id=tenant, model="claude-haiku-5-5"
        ),
        ma_agent(id="pricey-source", name="pricey", tenant_id=tenant, model="claude-opus-5-5"),
        ma_agent(id="kept-source", name="kept", tenant_id=tenant, model="claude-haiku-5-5"),
    ]
    restricted = policy.model_copy(
        update={"agent_rules": {"kept": AgentRule(runs_in=("another-channel",))}}
    )
    value = any_agent(grant) if mode == "any" else grant

    async def listing(client, *, tenant_id):
        assert tenant_id == tenant
        return agents

    async def read_grant(deps, admission, *, now):
        return value

    monkeypatch.setattr(ma_index, "list_agents_by_tenant", listing)
    monkeypatch.setattr(runtime_manifest, "read_agent_fleet_grant", read_grant)
    ports = HostFleetPorts(
        sessions,
        prices={},
        markup=Decimal(1),
        deps=SimpleNamespace(anthropic=None, fleet_enabled=False),
        preparation=lambda parent: None,
    )

    @asynccontextmanager
    async def root_request(parent, deadline=None):
        yield SimpleNamespace(admission=SimpleNamespace())

    monkeypatch.setattr(ports, "_load_preparation", root_request)
    listed = await ports.worker_agents(root, restricted)
    names = [row["name"] for row in listed]
    assert names == (["helper", "researcher"] if mode == "any" else ["helper"])
    assert listed[0]["root"] is True and listed[0]["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize("fault", [None, "refused", "foreign_capture"])
async def test_prepare_worker_agent_decides_admission_again_for_that_agent(
    captured, owned, monkeypatch, fault
):
    from types import SimpleNamespace

    from daimon.core import mux_compat
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.scope import ResolvedConfig
    from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
    from daimon.core.turn import admission as admission_module
    from daimon.core.turn import runtime_manifest
    from daimon.core.turn.admission import Admission, prepare_worker_agent
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.prepare import ProviderPreparationRequest

    source, admission, _, fleet, _, _ = captured
    _, identity, actor, sessions = owned
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    root_agent = ma_agent(id="root-source", name="helper", tenant_id=identity.tenant_id)
    root = ProviderPreparationRequest(
        SimpleNamespace(anthropic=None, sessionmaker=sessions, tool_safety=None),
        Admission(
            actor.account_id,
            root_agent,
            ma_environment(tenant_id=identity.tenant_id),
            ResolvedConfig(
                agent_name="helper",
                environment_name="default",
                responder_ma_agent_id="root-source",
            ),
            grant=admission,
            asks_before_publishing=True,
            backend_revision_origin="agent",
            origin_seal_ids=frozenset({"root-seal"}),
        ),
        fleet.scope,
        identity.tenant_id,
        "discord",
        "member",
        "thread",
        actor.account_id,
        False,
        DEFAULT_MA_CAPABILITIES,
        None,
        NOW + timedelta(minutes=5),
        lambda: NOW,
    )
    decided = []

    async def retrieve(client, agent_id, *, scope):
        assert agent_id == "researcher-source" and scope == fleet.scope
        return actual

    async def reauthorize(deps, again):
        decided.append(again)
        if fault == "refused":
            raise AdmissionDenied(reason="invoker_not_allowed")
        return again

    async def capture(request):
        assert request.admission.agent is actual and request.source is None
        if fault == "foreign_capture":
            return researcher.model_copy(update={"agent_id": uuid4()})
        return researcher

    async def rules(*args, **kwargs):
        return SimpleNamespace(tools=frozenset({"post_message"}), needs_driver=True)

    async def skills(*args, **kwargs):
        return ()

    monkeypatch.setattr(mux_compat, "retrieve_agent", retrieve)
    monkeypatch.setattr(admission_module, "reauthorize", reauthorize)
    monkeypatch.setattr(runtime_manifest, "capture_preparation_source", capture)
    monkeypatch.setattr(admission_module, "daimon_rule_set_for", rules)
    monkeypatch.setattr(admission_module, "turn_channel_skills", skills)
    if fault == "refused":
        with pytest.raises(AdmissionDenied):
            await prepare_worker_agent(root.deps, root, researcher)
        return
    if fault == "foreign_capture":
        with pytest.raises(AgentBackendRefused):
            await prepare_worker_agent(root.deps, root, researcher)
        return
    worker = await prepare_worker_agent(root.deps, root, researcher)
    (again,) = decided
    # The same requester, run place and scope; the agent and its facts are the worker's own.
    assert worker.admission.agent is actual and again.agent is actual
    assert worker.admission.grant.subject == admission.subject
    assert worker.admission.grant.run_place == admission.run_place
    assert worker.admission.grant.agent != admission.agent
    assert worker.admission.config.agent_name == "researcher"
    assert worker.admission.config.responder_ma_agent_id == actual.id
    assert worker.admission.daimon_rule_tools == frozenset({"post_message"})
    assert not worker.admission.asks_before_publishing
    assert worker.admission.backend_revision_origin is None
    assert worker.admission.origin_seal_ids == frozenset({"root-seal"})
    assert worker.scope == root.scope and worker.source is researcher
    assert root.admission.agent is root_agent  # the root's own request is untouched
