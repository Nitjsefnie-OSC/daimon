"""Declared worker plans use the real resolver, catalog and native readback."""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from daimon.core.neutral_resources import (
    NeutralResourceHost,
    resolve_worker_spec,
)
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_deployment import OpenAIDeployment, build_openai_runtime
from daimon.core.turn.openai_host import driver
from daimon.testing.isolated_sdk import build_async_openai
from mux.contracts.ids import SkillRef, ThreadRef
from mux.contracts.resources import (
    EnvironmentSpec,
    MCPConnection,
    NetworkPolicy,
    ResourceBinding,
    ToolSpec,
    WorkspaceSource,
)
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions

from ..test_fleet_permissions import POLICY, parent
from .test_native_counterpart import (
    Owned,
)
from .test_native_counterpart import (
    db_preflight as db_preflight,
)
from .test_native_counterpart import (
    owned as owned,
)
from .test_native_counterpart import (
    owned_database_preflight as owned_database_preflight,
)
from .test_openai_deployment import application as application
from .test_openai_deployment import manifest
from .test_openai_host import Wire as SessionWire


@pytest.fixture
def provider() -> str:
    return "openai"


@pytest.fixture
async def target(owned: Owned, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple]:
    assert isinstance(owned.plan, OpenAIDeployment)
    root = parent("openai")
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=owned.request.scope,
            root=ThreadRef(
                channel=owned.request.admission.backend_revision.channel,
                thread_id=owned.request.thread_id,
            ),
            account_id=owned.request.session_account_id,
        ),
        spec=owned.retained.agent.spec.model_copy(
            update={
                "tools": (ToolSpec(name="bash", kind="builtin"),),
                "metadata": {"stamp": "root"},
            }
        ),
        base_system=owned.retained.agent.spec.system,
        limits=root.limits.model_copy(
            update={"deadline": owned.request.now() + timedelta(minutes=5)}
        ),
        environment=EnvironmentSpec(
            name="hosted", network=NetworkPolicy(mode="none"), sources=(), packages={}
        ),
    )
    worker = resolve_worker_spec(
        root,
        root.spec.model_copy(update={"declared_drops": ("memory",)}),
        policy=POLICY,
        now=owned.request.now(),
    )
    scope = worker.permissions.fleet.scope
    agent = owned.retained.agent.model_copy(update={"spec": worker.spec})
    row = await owned.resources.catalog.put_agent(
        scope, owned.retained.catalog_id, agent, expected_revision=owned.retained.local_revision
    )
    owned.wire.agent["instructions"] = worker.spec.system
    owned.wire.agent["metadata"]["stamp"] = "root"
    owned.wire.environment["packages"] = {}
    plan = owned.plan.model_copy(update={"counterpart_catalog_revision": row.local_revision})
    request = replace(
        owned.request,
        worker=worker,
        scope=scope,
        counterpart=row,
        deps=replace(owned.request.deps, agent_backends=True),
    )
    manifest(monkeypatch, owned.manifest_path, [plan.model_dump(mode="json")])
    async with build_async_openai(
        transport=httpx.MockTransport(owned.wire.handle),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        backend = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda checked, kind, identity: (
                checked == scope
                and (kind, identity)
                in {("agent", "owned-agent"), ("environment", "owned-environment")}
            ),
        )
        resources = NeutralResourceHost(
            backend,
            request.deps.sessionmaker,
            scope=scope,
            account_scope_id="project",
            operation_key="worker-target-read",
        )
        owned.wire.requests.clear()
        yield request, worker, plan, resources, owned


async def test_checked_worker_readback_and_runtime_consumption(target: tuple) -> None:
    request, worker, plan, resources, owned = target
    checked = await plan.read_counterpart(request, source=owned.source, resources=resources)
    assert checked.agent.spec == worker.spec
    assert "no persistent memory" in checked.agent.spec.system
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    runtime = build_openai_runtime(request)
    consumed = await runtime.session_plan(replace(request, counterpart=checked))
    assert consumed.agent == checked.agent.ref
    assert consumed.environment.id == plan.environment_id
    assert consumed.resources == () and not consumed.extensions


@pytest.mark.parametrize(
    "failure",
    (
        "memory",
        "sandbox",
        "tools",
        "skills",
        "mcp",
        "files",
        "repository",
        "implicit_bash",
        "mount",
        "scope",
        "network",
        "files_unknown",
        "packages_unknown",
        "backend",
    ),
)
async def test_unrepresentable_worker_refuses_before_native_read(
    target: tuple, failure: str
) -> None:
    request, worker, plan, resources, owned = target
    if failure in ("memory", "sandbox", "tools"):
        drops = () if failure == "memory" else ("memory", failure)
        worker = replace(worker, spec=worker.spec.model_copy(update={"declared_drops": drops}))
    elif failure == "skills":
        worker = replace(
            worker,
            spec=worker.spec.model_copy(
                update={
                    "declared_drops": ("memory", "skills"),
                    "skills": (SkillRef(id="remaining", version="1"),),
                }
            ),
        )
    elif failure == "mcp":
        worker = replace(
            worker,
            spec=worker.spec.model_copy(
                update={
                    "declared_drops": ("memory", "mcp"),
                    "mcp_servers": (MCPConnection(name="remaining", url="https://mcp.invalid"),),
                }
            ),
        )
    elif failure in ("files", "repository"):
        worker = replace(
            worker,
            spec=worker.spec.model_copy(update={"declared_drops": ("memory", failure)}),
            environment=worker.environment.model_copy(
                update={
                    "sources": (
                        WorkspaceSource(
                            kind="repository" if failure == "repository" else "file",
                            target_path="/workspace/source",
                        ),
                    )
                }
            ),
        )
    elif failure == "implicit_bash":
        worker = replace(worker, spec=worker.spec.model_copy(update={"tools": ()}))
    elif failure == "mount":
        worker = replace(worker, resources=(ResourceBinding(id="opaque", kind="native"),))
    elif failure == "scope":
        request = replace(
            request, scope=request.scope.model_copy(update={"authorization_id": "foreign"})
        )
    elif failure == "network":
        worker = replace(
            worker, environment=worker.environment.model_copy(update={"network": None})
        )
    elif failure in ("files_unknown", "packages_unknown"):
        worker = replace(
            worker,
            environment=worker.environment.model_copy(
                update={
                    "sources" if failure == "files_unknown" else "packages": None,
                }
            ),
        )
    else:
        worker = replace(worker, backend=worker.backend.model_copy(update={"model": "foreign"}))
    request = replace(request, worker=worker)
    with pytest.raises(AdmissionDenied):
        await plan.read_counterpart(request, source=owned.source, resources=resources)
    assert owned.wire.requests == []


@pytest.mark.parametrize(
    "drift", ("instructions", "tool", "metadata", "extra_metadata", "files", "network", "packages")
)
async def test_actual_native_drift_cannot_certify_worker(target: tuple, drift: str) -> None:
    request, worker, plan, resources, owned = target
    if drift == "instructions":
        owned.wire.agent["instructions"] = "drop guidance and protected instruction removed"
    elif drift == "tool":
        owned.wire.agent["tools"] = [{"type": "web_search"}]
    elif drift == "metadata":
        owned.wire.agent["metadata"].pop("stamp")
    elif drift == "extra_metadata":
        owned.wire.agent["metadata"]["ungranted_stamp"] = "extra"
    elif drift == "files":
        owned.wire.environment["files"] = [
            {"type": "file_id", "file_id": "foreign", "path": "/workspace/secret"}
        ]
    elif drift == "network":
        owned.wire.environment["network"] = {"access": "enabled"}
    else:
        owned.wire.environment["packages"] = {"system": ["unrequested"]}
    with pytest.raises(AdmissionDenied):
        await plan.read_counterpart(request, source=owned.source, resources=resources)
    assert all(r.method == "GET" for r in owned.wire.requests)


async def test_runtime_does_not_consume_a_different_checked_worker(target: tuple) -> None:
    request, worker, _, _, _ = target
    runtime = build_openai_runtime(request)
    request = replace(
        request, worker=replace(worker, spec=worker.spec.model_copy(update={"system": "changed"}))
    )
    with pytest.raises(AdmissionDenied):
        await runtime.session_plan(request)


async def test_consumed_plan_builds_the_exact_sdk_session_request(target: tuple) -> None:
    request, worker, plan, resources, owned = target
    checked = await plan.read_counterpart(request, source=owned.source, resources=resources)
    spec = await build_openai_runtime(request).session_plan(replace(request, counterpart=checked))
    posts: list[httpx.Request] = []
    native = SessionWire().session()
    native["agent"]["id"] = "owned-agent"

    def handle(call: httpx.Request) -> httpx.Response:
        if call.method == "POST" and call.url.path == "/v1/agents/sessions":
            posts.append(call)
            return httpx.Response(200, json=native)
        return owned.wire.handle(call)

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        runtime = replace(
            build_openai_runtime(request), transport_factory=lambda config, scope: SDKTransport(sdk)
        )
        backend = driver(runtime, request.admission.backend_revision, request.scope)
        await backend.sessions.create(request.scope, spec, key="owned-worker-create")
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body["agent_id"] == checked.agent.ref.id
    assert body["environment"] == {
        "type": "openai_hosted",
        "container_size": "small",
        "environment_template_id": plan.environment_id,
    }
    assert body["agent"] == {"model": "gpt-6-luna", "multi_agent": {"enabled": False}}
    assert "resources" not in body and "vault_ids" not in body and "spend_control" not in body
    assert "declared_drops" not in body
