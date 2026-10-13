"""Installed runtime readers execute the owned counterpart proof at the host edge."""

import json
import os
from contextlib import AsyncExitStack
from dataclasses import replace

import httpx
import pytest
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings, TurnSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.turn import gemini_deployment, openai_deployment
from daimon.core.turn.deps import build_turn_deps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.runtime_manifest import read_preparation_counterpart
from daimon.testing.isolated_sdk import build_async_openai, build_gemini_client
from mux.drivers.gemini import deployment as gemini_transport
from mux.drivers.gemini.transport import SDKTransport as GeminiSDKTransport
from mux.drivers.openai import deployment as openai_transport
from mux.drivers.openai.transport import SDKTransport
from pydantic import PostgresDsn, SecretStr

from .test_native_counterpart import (
    application as application,
)
from .test_native_counterpart import (
    clean_sdk_environment as clean_sdk_environment,
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
from .test_native_counterpart import (
    provider as provider,
)
from .test_openai_host import Wire as OpenAIWire
from .test_openai_worker_plan import target as target


@pytest.fixture
async def native_request(owned, monkeypatch):
    selected = owned.request
    agent = selected.admission.agent.model_copy(
        update={
            "metadata": {
                MA_METADATA_KEY_TENANT: str(selected.tenant_id),
                MA_METADATA_KEY_NAME: selected.admission.config.agent_name,
            }
        }
    )
    selected = replace(
        selected,
        admission=replace(selected.admission, agent=agent),
        deps=replace(selected.deps, agent_backends=True),
    )
    original = os.environ.get
    monkeypatch.setattr(
        os.environ,
        "get",
        lambda name, default=None: (
            "offline" if name == owned.plan.api_key_env else original(name, default)
        ),
    )

    class TurnWire(OpenAIWire):
        def session(self):
            raw = super().session()
            raw["agent"]["id"] = owned.retained.agent.ref.id
            return raw

    turn_wire = TurnWire()
    turn_wire.expected_spend_limit = None
    posts = []

    def native(request):
        if request.method == "GET" and (
            request.url.path.startswith("/v1beta/agents/")
            or request.url.path == "/v1/agents/owned-agent"
            or "/environments/templates/" in request.url.path
        ):
            return owned.wire.handle(request)
        if owned.wire.provider == "openai":
            return turn_wire.handle(request)
        if request.url.path == "/v1beta/interactions" and request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": f"interaction-{len(posts)}",
                    "status": "completed",
                    "environment_id": "workspace",
                    "outputs": [],
                    "usage": {
                        "total_input_tokens": 12,
                        "total_output_tokens": 3,
                        "total_thought_tokens": 0,
                    },
                },
            )
        raise AssertionError("unexpected native execution request")

    async with AsyncExitStack() as stack:
        if owned.wire.provider == "openai":
            sdk = await stack.enter_async_context(
                build_async_openai(
                    transport=httpx.MockTransport(native),
                    api_key="offline",
                    base_url="https://openai.invalid/v1",
                )
            )
            transport = SDKTransport(sdk)
        else:
            sdk = build_gemini_client(transport=httpx.MockTransport(native), api_key="offline")
            stack.callback(sdk.close)
            stack.push_async_callback(sdk.aio.aclose)
            transport = GeminiSDKTransport(sdk)
        selected, runtime = install_runtime(selected, owned, monkeypatch, transport)
        yield selected, runtime, turn_wire, posts


def install_runtime(selected, owned, monkeypatch, transport):
    if owned.wire.provider == "openai":
        monkeypatch.setattr(
            openai_transport,
            "configured_transport",
            lambda **kwargs: transport,
        )
        runtime = openai_deployment.build_openai_runtime(selected)
    else:
        source = owned.resources.backend.agents
        monkeypatch.setattr(
            gemini_deployment, "SQLiteGeminiStorage", lambda *args, **kwargs: source._storage
        )
        monkeypatch.setattr(gemini_transport, "configured_transport", lambda **kwargs: transport)
        monkeypatch.setattr(
            gemini_transport,
            "configured_catalog_transport",
            lambda **kwargs: source._catalog_transport,
        )
        runtime = gemini_deployment.build_gemini_runtime(selected)
    selected = replace(
        selected,
        deps=replace(
            selected.deps, turn_runtimes={selected.admission.backend_revision.profile: runtime}
        ),
    )
    return selected, runtime


async def test_installed_runtime_reader_uses_real_scoped_catalog_and_sdk_readback(
    native_request, owned
):
    request, runtime, _, _ = native_request
    assert runtime.read_agent_counterpart is not None
    row = await runtime.read_agent_counterpart(request)
    assert row == owned.retained
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    if owned.wire.provider == "openai":
        planned = await runtime.session_plan(replace(request, counterpart=row))
        assert planned.agent == row.agent.ref and planned.agent_revision.local == 0


@pytest.mark.parametrize(
    "failure",
    [
        "source_version",
        "native_id",
        "catalog_revision",
        "revoked",
        "flag_off",
        "native_drift",
        "native_missing",
    ],
)
async def test_installed_runtime_proof_failures_have_zero_native_mutations(
    native_request, owned, failure
):
    request, _, _, _ = native_request
    if failure == "source_version":
        request = replace(
            request,
            admission=replace(
                request.admission, agent=request.admission.agent.model_copy(update={"version": 2})
            ),
        )
    elif failure == "flag_off":
        request = replace(request, deps=replace(request.deps, agent_backends=False))
    elif failure == "native_drift":
        if owned.wire.provider == "gemini":
            owned.wire.agent["tools"] = [{"type": "google_search"}]
        else:
            owned.wire.agent["model"] = "foreign-model"
    elif failure == "native_missing":
        owned.wire.error = ("/v1/agents/owned-agent", 404)
    else:
        import json

        values = owned.plan.model_dump(mode="json")
        if failure == "native_id":
            values["agent_id"] = "foreign"
        elif failure == "catalog_revision":
            values["counterpart_catalog_revision"] = 2
        else:
            values = None
        owned.manifest_path.write_text(json.dumps([] if values is None else [values]))
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(request)
    assert not owned.wire.requests, "unproved source refuses before target drift reads"


async def test_real_host_refuses_complete_native_component_as_source_execution_proof(
    native_request, owned
):
    request, runtime, wire, posts = native_request
    assert runtime.read_agent_counterpart is not None
    row = await runtime.read_agent_counterpart(request)
    assert row == owned.retained
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    owned.wire.requests.clear()
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await bind_session_impl(
            request.deps,
            replace(request.admission, backend_counterpart=row),
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=True,
            deadline=request.deadline,
            now=request.now,
        )
    assert owned.wire.requests == []
    assert wire.requests == [] and posts == []
    assert runtime is not None


@pytest.mark.parametrize("enabled", [False, True])
async def test_explicit_execution_setting_reaches_real_dependencies_without_allocating_runtime(
    application, enabled
):
    settings = Settings(
        anthropic=AnthropicSettings(api_key=SecretStr("offline")),
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://offline/unused")),
        turn=TurnSettings(agent_backends=enabled),
    )
    deps = build_turn_deps(
        settings,
        application.deps.anthropic,
        application.deps.sessionmaker,
        deployment_default=application.deps.deployment_default,
        resolver_cache=application.deps.resolver_cache,
        billing_config=None,
    )
    assert deps.agent_backends is enabled
    assert deps.turn_path == "legacy" and deps.channel_backends is False
    assert deps.turn_runtimes == {} and deps.state_store is None


@pytest.mark.parametrize("obligation", ["skill", "mcp", "instructions", "tool_approval"])
async def test_real_host_refuses_source_obligations_missing_from_installed_counterpart(
    native_request, owned, obligation
):
    request, _, _, _ = native_request
    values = request.admission.agent.model_dump()
    if obligation == "skill":
        values["skills"] = [{"type": "custom", "skill_id": "required-source-skill", "version": "1"}]
    elif obligation == "mcp":
        values["mcp_servers"] = [
            {"type": "url", "name": "required-source-mcp", "url": "https://mcp.invalid/mcp"}
        ]
    elif obligation == "tool_approval":
        values["tools"] = [
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}}
                ],
                "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            }
        ]
    else:
        values["system"] = "Required source instructions absent from counterpart"
    agent = type(request.admission.agent).model_validate(values)
    request = replace(request, admission=replace(request.admission, agent=agent))
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=True,
            deadline=request.deadline,
            now=request.now,
        )
    assert not any(r.method != "GET" for r in owned.wire.requests)


@pytest.mark.parametrize(
    "obligation",
    [
        "instructions",
        "tools",
        "skills",
        "mcp",
        "credentials",
        "files",
        "repository",
        "memory",
        "environment",
    ],
)
async def test_complete_source_obligation_capture_refuses_before_target_io(
    native_request, owned, obligation, monkeypatch
):
    from daimon.core import sessions
    from daimon.core._models import AgentFile
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_mcp_credentials, agent_memory_stores, agent_repo_binding
    from mux.errors import UnsupportedCapability

    request, _, wire, posts = native_request
    baseline_values = request.admission.agent.model_dump()
    baseline_values.update(system=None, tools=[], skills=[], mcp_servers=[])
    baseline_agent = type(request.admission.agent).model_validate(baseline_values)
    request = replace(
        request,
        admission=replace(request.admission, agent=baseline_agent),
        deps=replace(request.deps, agent_github_app=None),
    )
    with pytest.raises(AdmissionDenied) as baseline_refusal:
        await read_preparation_counterpart(request)
    baseline = baseline_refusal.value.__cause__
    assert isinstance(baseline, UnsupportedCapability)
    assert set(baseline.missing) == {"memory", "environment"}
    assert obligation not in baseline.missing or obligation in {"memory", "environment"}
    assert owned.wire.requests == [] and wire.requests == [] and posts == []
    source_id = derive_agent_uuid(
        tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
    )
    values = request.admission.agent.model_dump()
    if obligation == "instructions":
        values["system"] = "Source instructions must survive"
    elif obligation == "tools":
        values["tools"] = [
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}}
                ],
                "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            }
        ]
    elif obligation == "skills":
        values["skills"] = [{"type": "custom", "skill_id": "required-source-skill", "version": "1"}]
    elif obligation == "mcp":
        values["mcp_servers"] = [
            {"type": "url", "name": "required-source-mcp", "url": "https://mcp.invalid/mcp"}
        ]
    agent = type(request.admission.agent).model_validate(values)
    request = replace(request, admission=replace(request.admission, agent=agent))
    async with request.deps.sessionmaker.begin() as db:
        if obligation == "credentials":
            await agent_mcp_credentials.upsert_credential(
                db,
                tenant_id=request.tenant_id,
                agent_id=source_id,
                mcp_server_url="https://mcp.invalid/mcp",
                encrypted_token=b"unread-credential-marker",
            )
        elif obligation == "files":
            db.add(
                AgentFile(
                    tenant_id=request.tenant_id,
                    agent_id=source_id,
                    key="PRIVATE_SAMPLE",
                    content="intentionally-invalid-ciphertext",
                    encoding="fernet_v1",
                )
            )
        elif obligation == "repository":
            await agent_repo_binding.set_binding(
                db,
                tenant_id=request.tenant_id,
                agent_id=source_id,
                repo_url="example/source",
                default_branch="main",
                ma_secret_ref="opaque-source-credential",
                proof=None,
            )
        assert (
            await agent_memory_stores.get_memory_store_id(
                db,
                tenant_id=request.tenant_id,
                agent_id=source_id,
            )
            is None
        )
    if obligation == "credentials":
        from cryptography.fernet import Fernet, MultiFernet
        from daimon.core.config import McpSettings

        request = replace(
            request,
            deps=replace(
                request.deps,
                fernet=MultiFernet([Fernet(Fernet.generate_key())]),
                mcp=McpSettings(public_url="https://mcp.invalid", jwt_secret=SecretStr("offline")),
            ),
        )
    if obligation == "environment":
        raw = request.admission.environment.model_dump()
        raw["config"]["packages"]["pip"] = ["required-source-package==1"]
        environment = type(request.admission.environment).model_validate(raw)
        request = replace(request, admission=replace(request.admission, environment=environment))
    captured = []
    original = sessions.project_session_resources

    async def capture(*args, **kwargs):
        result = await original(*args, **kwargs)
        captured.append(result)
        return result

    def no_decryption(*args, **kwargs):
        raise AssertionError("source projection decoded a secret")

    monkeypatch.setattr(sessions, "project_session_resources", capture)
    monkeypatch.setattr("daimon.core.stores.agent_files._cipher", no_decryption)
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    detail = refused.value.__cause__
    assert isinstance(detail, UnsupportedCapability)
    expected = {"memory", "environment", obligation}
    if obligation == "files":
        expected.add("credentials")
    assert set(detail.missing) == expected
    assert len(captured) == 1
    source = captured[0]
    assert source.source is not None and source.source.ref.id == agent.id
    assert source.source.ref.account_scope_id == "", "unknown SDK workspace cannot be fabricated"
    assert source.resource_scope == request.scope
    assert source.effective_spec is not None
    assert source.memory_required is True and source.memory_store_id is None
    assert (
        source.memory_instructions and source.memory_read_only is request.admission.memory_read_only
    )
    if obligation == "instructions":
        assert source.effective_spec.system == values["system"]
    elif obligation == "tools":
        extension = next(
            e for e in source.effective_spec.extensions if e.namespace == "anthropic.agent_tools"
        )
        assert (
            extension.value["tools"][0]["configs"][0]["permission_policy"]["type"] == "always_ask"
        )
    elif obligation == "skills":
        assert source.effective_spec.skills[0].id == "required-source-skill"
    elif obligation == "mcp":
        assert source.effective_spec.mcp_servers[0].name == "required-source-mcp"
    elif obligation in ("files", "credentials"):
        assert source.resource_requirements and source.resource_generations
    elif obligation == "repository":
        assert source.repository_sources[0].repository_url == "https://github.com/example/source"
    elif obligation == "environment":
        assert source.environment is not None
        assert source.environment.native["config"]["packages"]["pip"] == [
            "required-source-package==1"
        ]
    assert source.model_dump() == {
        key: value
        for key, value in source.model_dump().items()
        if key
        not in {
            "source",
            "effective_spec",
            "environment",
            "resource_scope",
            "resource_requirements",
            "repository_sources",
            "resource_generations",
            "memory_required",
            "memory_store_id",
            "memory_read_only",
            "memory_instructions",
        }
    }
    assert "Source instructions must survive" not in repr(source)
    assert "intentionally-invalid-ciphertext" not in repr(source)
    assert owned.wire.requests == [] and wire.requests == [] and posts == []


async def test_opaque_caller_resource_is_an_unproved_source_definition(native_request, owned):
    from mux.errors import UnsupportedCapability

    request, _, wire, posts = native_request
    request = replace(
        request, admission=replace(request.admission, private_dm_id="caller-private-context")
    )
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    detail = refused.value.__cause__
    assert isinstance(detail, UnsupportedCapability)
    assert {"source_definition", "memory", "environment"} <= set(detail.missing)
    assert owned.wire.requests == [] and wire.requests == [] and posts == []


@pytest.fixture
async def explicit_worker_request(owned, monkeypatch):
    from datetime import timedelta

    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.neutral_resources import resolve_worker_spec
    from mux.contracts.ids import ThreadRef

    from ..test_fleet_permissions import POLICY, parent

    request = replace(
        owned.request,
        admission=replace(
            owned.request.admission,
            agent=owned.request.admission.agent.model_copy(
                update={
                    "metadata": {
                        MA_METADATA_KEY_TENANT: str(owned.request.tenant_id),
                        MA_METADATA_KEY_NAME: owned.request.admission.config.agent_name,
                    }
                }
            ),
        ),
    )
    source_values = request.admission.agent.model_dump()
    source_values.update(
        system="Retained instructions",
        skills=[{"type": "custom", "skill_id": "source-required-skill", "version": "1"}],
        mcp_servers=[
            {"type": "url", "name": "source-required-mcp", "url": "https://mcp.invalid/mcp"}
        ],
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}}
                ],
                "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            }
        ],
    )
    request = replace(
        request,
        admission=replace(
            request.admission, agent=type(request.admission.agent).model_validate(source_values)
        ),
    )
    root = parent(owned.wire.provider)
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=request.scope,
            root=ThreadRef(
                channel=request.admission.backend_revision.channel, thread_id=request.thread_id
            ),
            account_id=request.admission.account_id,
        ),
        agent_id=derive_agent_uuid(
            tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
        ),
        spec=owned.retained.agent.spec,
        base_system=owned.retained.agent.spec.system,
        limits=root.limits.model_copy(update={"deadline": request.now() + timedelta(seconds=20)}),
    )
    worker = resolve_worker_spec(
        root,
        root.spec.model_copy(
            update={
                "declared_drops": (
                    "memory",
                    "sandbox",
                    "skills",
                    "mcp",
                    "tools",
                    "files",
                    "repository",
                )
            }
        ),
        policy=POLICY,
        now=request.now(),
    )
    scope = worker.permissions.fleet.scope
    agent = await owned.resources.backend.agents.create(scope, worker.spec, key="restricted-worker")
    row = await owned.resources.catalog.put_agent(
        scope,
        owned.retained.catalog_id,
        agent,
        expected_revision=owned.retained.local_revision,
    )
    plan = owned.plan.model_copy(
        update={
            "agent_name": worker.spec.name,
            "system": worker.spec.system,
            "tools": (),
            "agent_id": agent.ref.id,
            "skill_ids": (),
            "counterpart_catalog_revision": row.local_revision,
        }
    )
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    request = replace(
        request,
        scope=scope,
        worker=worker,
        deps=replace(request.deps, agent_backends=True, agent_github_app=None),
    )
    from .test_gemini_codec import TrackedTransport

    transport = TrackedTransport()
    original = os.environ.get
    monkeypatch.setattr(
        os.environ,
        "get",
        lambda name, default=None: (
            "offline" if name == plan.api_key_env else original(name, default)
        ),
    )
    request, runtime = install_runtime(request, owned, monkeypatch, transport)
    owned.wire.requests.clear()
    yield request, runtime, owned, transport


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_complete_declared_worker_proof_consumes_actual_native_counterpart(
    explicit_worker_request,
):
    request, runtime, owned, transport = explicit_worker_request
    selected = await read_preparation_counterpart(request)
    assert selected.deadline == min(request.deadline, request.worker.limits.deadline)
    assert selected.worker == request.worker
    assert selected.source.resource_scope == request.scope
    assert selected.source.memory_required is True
    assert selected.counterpart.agent.spec == request.worker.spec
    assert selected.counterpart.agent.ref.id == owned.wire.native_id
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    prepared = await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
        deadline=request.deadline,
        now=request.now,
        worker=request.worker,
    )
    assert prepared.backend is not None
    assert prepared.runtime.journal.deployment.native_agent == selected.counterpart.agent
    assert prepared.admission.backend_revision_origin == "agent"
    assert prepared.runtime.journal.deployment.agent == request.worker.spec
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart

    from .test_gemini_codec import interaction

    transport.responses.append(interaction("verified-worker-turn", status="completed"))
    await prepared.backend.events.send(
        request.scope,
        prepared.session_ref,
        (UserMessage(content=(TextPart(text="run the restricted task"),)),),
        key="worker-input",
    )
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent["agent"] == selected.counterpart.agent.ref.id
    assert sent["tools"] == []
    assert sent["environment"] == {"type": "remote", "sources": []}


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize(
    "failure",
    [
        "memory",
        "sandbox",
        "instructions",
        "tools",
        "skills",
        "mcp",
        "repository",
        "ask_without_tools",
        "credentials",
        "files",
        "private",
        "scope",
        "expired",
        "foreign_root",
        "missing_reader",
        "flag_off",
        "native_drift",
        "source_drift",
        "model_controls",
    ],
)
async def test_explicit_drops_do_not_discharge_unproved_obligations(
    explicit_worker_request, failure, monkeypatch
):
    from daimon.core import sessions
    from daimon.core._models import AgentFile
    from mux.contracts.resources import ResourceBinding

    request, runtime, owned, transport = explicit_worker_request
    worker = request.worker
    if failure in ("tools", "skills", "mcp", "repository"):
        if failure == "repository":
            from daimon.core.stores import agent_repo_binding

            async with request.deps.sessionmaker.begin() as db:
                await agent_repo_binding.set_binding(
                    db,
                    tenant_id=request.tenant_id,
                    agent_id=worker.permissions.agent_id,
                    repo_url="example/source",
                    default_branch="main",
                    ma_secret_ref="unread",
                    proof=None,
                )
        removed = {failure, "tools"} if failure == "mcp" else {failure}
        spec = worker.spec.model_copy(
            update={"declared_drops": tuple(x for x in worker.declared_drops if x not in removed)}
        )
        worker = replace(worker, spec=spec, permissions=replace(worker.permissions, spec=spec))
    elif failure in ("memory", "sandbox"):
        spec = worker.spec.model_copy(
            update={"declared_drops": tuple(x for x in worker.declared_drops if x != failure)}
        )
        worker = replace(worker, spec=spec, permissions=replace(worker.permissions, spec=spec))
    elif failure == "instructions":
        agent = request.admission.agent.model_copy(
            update={"system": "Protected source instructions"}
        )
        request = replace(request, admission=replace(request.admission, agent=agent))
    elif failure == "ask_without_tools":
        request = replace(
            request,
            admission=replace(
                request.admission,
                asks_before_publishing=True,
                agent=request.admission.agent.model_copy(update={"tools": []}),
            ),
        )
    elif failure == "model_controls":
        agent = request.admission.agent.model_copy(
            update={"model": request.admission.agent.model.model_copy(update={"speed": "fast"})}
        )
        request = replace(request, admission=replace(request.admission, agent=agent))
    elif failure == "source_drift":
        read = runtime.read_agent_counterpart

        async def drift(selected):
            checked = await read(selected)
            async with request.deps.sessionmaker.begin() as db:
                db.add(
                    AgentFile(
                        tenant_id=request.tenant_id,
                        agent_id=worker.permissions.agent_id,
                        key="LATE_PRIVATE",
                        content="invalid-ciphertext",
                        encoding="fernet_v1",
                    )
                )
            return checked

        request = replace(
            request,
            deps=replace(
                request.deps,
                turn_runtimes={
                    request.admission.backend_revision.profile: replace(
                        runtime, read_agent_counterpart=drift
                    )
                },
            ),
        )
    elif failure in ("credentials", "files"):
        if failure == "files":
            spec = worker.spec.model_copy(
                update={"declared_drops": tuple(x for x in worker.declared_drops if x != "files")}
            )
            worker = replace(worker, spec=spec, permissions=replace(worker.permissions, spec=spec))
        async with request.deps.sessionmaker.begin() as db:
            db.add(
                AgentFile(
                    tenant_id=request.tenant_id,
                    agent_id=worker.permissions.agent_id,
                    key="UNREAD_PRIVATE",
                    content="invalid-ciphertext",
                    encoding="fernet_v1",
                )
            )
    elif failure == "private":
        capture = sessions.project_session_resources

        async def private(*args, **kwargs):
            source = await capture(*args, **kwargs)
            return source.model_copy(
                update={
                    "resource_requirements": (ResourceBinding(id="opaque-private", kind="native"),)
                }
            )

        monkeypatch.setattr(sessions, "project_session_resources", private)
    elif failure == "scope":
        request = replace(request, scope=request.scope.model_copy(update={"account_id": "foreign"}))
    elif failure == "expired":
        limits = worker.limits.model_copy(update={"deadline": request.now()})
        worker = replace(
            worker, limits=limits, permissions=replace(worker.permissions, limits=limits)
        )
    elif failure == "foreign_root":
        worker = replace(worker, permissions=replace(worker.permissions, agent_id=None))
    elif failure == "missing_reader":
        request = replace(
            request,
            deps=replace(
                request.deps,
                turn_runtimes={
                    request.admission.backend_revision.profile: replace(
                        runtime, read_agent_counterpart=None
                    )
                },
            ),
        )
    elif failure == "flag_off":
        request = replace(request, deps=replace(request.deps, agent_backends=False))
    elif failure == "native_drift":
        owned.wire.agent["system_instruction"] = "Dropped protected instructions"
    request = replace(request, worker=worker)
    with pytest.raises(AdmissionDenied, match="backend_unsupported") as refused:
        await read_preparation_counterpart(request)
    from mux.errors import UnsupportedCapability

    if failure == "expired":
        from daimon.core.errors import TurnError

        assert isinstance(refused.value.__cause__, TurnError)
        assert refused.value.__cause__.kind == "ceiling"
    elif failure not in {"native_drift", "scope", "flag_off"}:
        assert isinstance(refused.value.__cause__, UnsupportedCapability)
        expected = {
            "sandbox": "environment",
            "private": "source_definition",
            "expired": "worker_context",
            "foreign_root": "worker_context",
            "missing_reader": "native_counterpart",
            "source_drift": "source_drift",
            "model_controls": "source_definition",
            "ask_without_tools": "tools",
        }.get(failure, failure)
        assert expected in refused.value.__cause__.missing
    assert not any(r.method != "GET" for r in owned.wire.requests)
    assert transport.requests == []
    if failure not in {"native_drift", "source_drift"}:
        assert owned.wire.requests == []


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_worker_source_refusal_precedes_revision_publication(explicit_worker_request):
    from daimon.core.authz import Subject
    from daimon.core.channel_backend import AgentBackendService, BackendActor, resolve_turn_backend
    from daimon.core.stores import mux_state
    from sqlalchemy import text

    request, _, _, transport = explicit_worker_request
    selected = await read_preparation_counterpart(request)
    source = selected.source
    service = AgentBackendService(request.deps.sessionmaker, enabled=True)
    actor = BackendActor(request.tenant_id, request.session_account_id, Subject(is_admin=True))
    policy = await service.tenant_policy(request.tenant_id)
    await service.set_tenant_allowlist(
        request.tenant_id, ("anthropic", "gemini"), policy.revision, actor
    )
    await service.set_backend(source, "gemini", 0, actor)
    drops = tuple(x for x in request.worker.declared_drops if x != "skills")
    spec = request.worker.spec.model_copy(update={"declared_drops": drops})
    worker = replace(
        request.worker, spec=spec, permissions=replace(request.worker.permissions, spec=spec)
    )

    async def verify(selection, origin, revision):
        if revision is not None:
            candidate = replace(
                request,
                worker=worker,
                admission=replace(
                    request.admission, backend_revision=revision, backend_revision_origin=origin
                ),
            )
            await read_preparation_counterpart(candidate)

    channel = request.admission.backend_revision.channel
    async with request.deps.sessionmaker.begin() as db:
        with pytest.raises(AdmissionDenied):
            await resolve_turn_backend(db, channel, source, enabled=True, validate_origin=verify)
        assert await mux_state.latest_config_revision(db, channel) is None
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM channel_config_revision WHERE tenant_id=:tenant AND channel_id=:channel"
                ),
                {"tenant": request.tenant_id, "channel": channel.channel_id},
            )
            == 0
        )
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_worker_proof_cancellation_does_not_reach_preparation(explicit_worker_request):
    import asyncio

    request, runtime, owned, transport = explicit_worker_request

    async def cancelled(selected):
        raise asyncio.CancelledError

    request = replace(
        request,
        deps=replace(
            request.deps,
            turn_runtimes={
                request.admission.backend_revision.profile: replace(
                    runtime, read_agent_counterpart=cancelled
                )
            },
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        await read_preparation_counterpart(request)
    assert not owned.wire.requests and not transport.requests


@pytest.mark.parametrize("provider", ["openai"], indirect=True)
async def test_openai_hosted_component_cannot_erase_the_legacy_source_environment(
    target,
):
    from datetime import timedelta

    from daimon.core.ma_identity import derive_agent_uuid
    from mux.errors import UnsupportedCapability

    request, worker, _, _, owned = target
    agent = request.admission.agent.model_copy(
        update={
            "metadata": {
                MA_METADATA_KEY_TENANT: str(request.tenant_id),
                MA_METADATA_KEY_NAME: request.admission.config.agent_name,
            }
        }
    )
    limits = worker.limits.model_copy(update={"deadline": request.now() + timedelta(seconds=20)})
    worker = replace(
        worker,
        limits=limits,
        permissions=replace(
            worker.permissions,
            limits=limits,
            agent_id=derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=agent.id),
        ),
    )
    request = replace(
        request,
        worker=worker,
        admission=replace(request.admission, agent=agent),
        deps=replace(request.deps, agent_github_app=None),
    )
    with pytest.raises(AdmissionDenied) as refused:
        await read_preparation_counterpart(request)
    assert isinstance(refused.value.__cause__, UnsupportedCapability)
    assert "environment" in refused.value.__cause__.missing
    assert not owned.wire.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("route", ["channel", "legacy"])
async def test_worker_cannot_bypass_source_proof_through_channel_origin(
    explicit_worker_request, monkeypatch, route
):
    from daimon.core.turn import prepare as preparation

    request, _, owned, transport = explicit_worker_request

    async def forbidden(selected):
        raise AssertionError("a worker reached the preparer without its source proof")

    monkeypatch.setitem(
        preparation._PROVIDER_PREPARATIONS, request.admission.backend_revision.profile, forbidden
    )
    admission = replace(request.admission, backend_revision_origin="channel")
    if route == "legacy":
        from mux.contracts.config import ConfigRevision
        from mux.profiles import backend_selection

        admission = replace(
            request.admission,
            backend_revision=ConfigRevision.create(
                request.admission.backend_revision.channel, 1, backend_selection("anthropic")
            ),
        )
    with pytest.raises(AdmissionDenied):
        await bind_session_impl(
            request.deps,
            admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=True,
            deadline=request.deadline,
            now=request.now,
            worker=request.worker,
        )
    assert not owned.wire.requests and not transport.requests


async def _bind_worker(request, **kwargs):
    return await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
        deadline=request.deadline,
        now=request.now,
        worker=request.worker,
        **kwargs,
    )


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("entry", ["proof", "registered"])
async def test_worker_readback_cannot_outlive_its_shorter_authority(
    explicit_worker_request, monkeypatch, entry
):
    from datetime import timedelta

    from daimon.core.errors import TurnError
    from daimon.core.turn import prepare as preparation

    request, runtime, owned, transport = explicit_worker_request
    clock = [request.now()]
    bound = request.worker.limits.deadline
    assert bound < request.deadline
    prepared_callbacks = []
    preparer_calls = []
    readback_bounds = []

    async def slow_readback(selected):
        readback_bounds.append(selected.deadline)
        result = await runtime.read_agent_counterpart(selected)
        clock[0] = bound + timedelta(seconds=1)
        assert clock[0] < request.deadline
        return result

    async def forbidden(selected):
        preparer_calls.append(selected)
        raise AssertionError("expired proof reached native preparation")

    async def on_prepared(prepared):
        prepared_callbacks.append(prepared)

    profile = request.admission.backend_revision.profile
    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, forbidden)
    request = replace(
        request,
        now=lambda: clock[0],
        deps=replace(
            request.deps,
            turn_runtimes={profile: replace(runtime, read_agent_counterpart=slow_readback)},
        ),
    )
    with pytest.raises(AdmissionDenied) as refused:
        if entry == "proof":
            await read_preparation_counterpart(request)
        else:
            await _bind_worker(request, _on_prepared=on_prepared)
    assert isinstance(refused.value.__cause__, TurnError)
    assert refused.value.__cause__.kind == "ceiling"
    assert readback_bounds == [bound]
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert preparer_calls == [] and prepared_callbacks == [] and transport.requests == []


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_stricter_caller_bound_reaches_the_registered_preparer(
    explicit_worker_request, monkeypatch
):
    from datetime import timedelta

    from daimon.core.turn import prepare as preparation

    request, runtime, owned, transport = explicit_worker_request
    original_worker = request.worker
    bound = request.now() + timedelta(seconds=10)
    assert bound < original_worker.limits.deadline
    profile = request.admission.backend_revision.profile
    assert preparation.ensure_channel_runtime(
        profile, ready=lambda: profile in preparation._PROVIDER_PREPARATIONS
    )
    prepare = preparation._PROVIDER_PREPARATIONS[profile]
    seen = []

    async def bounded_preparation(selected):
        assert selected.deadline == bound
        assert selected.worker == original_worker
        seen.append(selected)
        return await prepare(selected)

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, bounded_preparation)
    selected = await read_preparation_counterpart(replace(request, deadline=bound))
    assert selected.deadline == bound and selected.worker == original_worker
    prepared = await _bind_worker(replace(request, deadline=bound))
    assert prepared.backend is not None and len(seen) == 1
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_hanging_final_source_capture_is_bounded_and_cancelled(
    explicit_worker_request, monkeypatch
):
    import asyncio
    from datetime import timedelta

    from daimon.core import sessions

    request, runtime, owned, transport = explicit_worker_request
    clock = [request.now()]
    bound = request.worker.limits.deadline

    async def readback(selected):
        result = await runtime.read_agent_counterpart(selected)
        clock[0] = bound - timedelta(seconds=0.05)
        return result

    request = replace(
        request,
        now=lambda: clock[0],
        deps=replace(
            request.deps,
            turn_runtimes={
                request.admission.backend_revision.profile: replace(
                    runtime, read_agent_counterpart=readback
                )
            },
        ),
    )
    capture = sessions.project_session_resources
    calls = []
    cancelled = []

    async def hanging_capture(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            return await capture(*args, **kwargs)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(sessions, "project_session_resources", hanging_capture)
    with pytest.raises(AdmissionDenied) as refused:
        await asyncio.wait_for(read_preparation_counterpart(request), timeout=1)
    assert isinstance(refused.value.__cause__, TimeoutError)
    assert len(calls) == 2 and cancelled == [True]
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("stage", ["capture", "preparation", "callback"])
async def test_expiry_is_rechecked_before_successful_worker_handoff(
    explicit_worker_request, monkeypatch, stage, capsys
):
    from datetime import timedelta

    from daimon.core import sessions
    from daimon.core.errors import TurnError
    from daimon.core.turn import prepare as preparation

    request, _, owned, transport = explicit_worker_request
    clock = [request.now()]
    bound = request.worker.limits.deadline
    request = replace(request, now=lambda: clock[0])
    profile = request.admission.backend_revision.profile
    assert preparation.ensure_channel_runtime(
        profile, ready=lambda: profile in preparation._PROVIDER_PREPARATIONS
    )
    prepare = preparation._PROVIDER_PREPARATIONS[profile]
    capture = sessions.project_session_resources
    capture_calls = []
    preparation_calls = []
    callbacks = []

    async def timed_capture(*args, **kwargs):
        result = await capture(*args, **kwargs)
        capture_calls.append(None)
        if stage == "capture" and len(capture_calls) == 2:
            clock[0] = bound + timedelta(seconds=1)
        return result

    async def timed_prepare(selected):
        assert selected.deadline == bound
        preparation_calls.append(selected)
        result = await prepare(selected)
        if stage == "preparation":
            clock[0] = bound + timedelta(seconds=1)
        return result

    async def on_prepared(prepared):
        callbacks.append(prepared)
        if stage == "callback":
            clock[0] = bound + timedelta(seconds=1)

    monkeypatch.setattr(sessions, "project_session_resources", timed_capture)
    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, timed_prepare)
    with pytest.raises((AdmissionDenied, TurnError)) as refused:
        await _bind_worker(request, _on_prepared=on_prepared)
    error = refused.value
    if isinstance(error, AdmissionDenied):
        error = error.__cause__
    assert isinstance(error, TurnError) and error.kind == "ceiling"
    assert len(preparation_calls) == (stage != "capture")
    assert len(callbacks) == (stage == "callback")
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_proven_worker_scope_survives_the_real_host_binding_and_codec(
    explicit_worker_request,
):
    from daimon.core.turn.binding import prepared_persistence
    from daimon.core.turn.run import _turn_port_kwargs

    request, _, _, _ = explicit_worker_request
    prepared = await _bind_worker(request)
    assert prepared._scope == request.worker.permissions.fleet.scope
    port = _turn_port_kwargs(
        request.deps,
        prepared.admission,
        prepared.ma_session_id,
        tenant_id=request.tenant_id,
        provider_backend=prepared.backend,
        provider_session=prepared.session_ref,
        provider_runtime=prepared.runtime,
        provider_scope=prepared._scope,
    )
    assert port["scope"] == request.worker.permissions.fleet.scope
    assert port["backend_request"].scope == port["scope"]
    persistence = await prepared_persistence(
        request.deps,
        prepared.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        thread_id=request.thread_id,
        session_id=prepared.ma_session_id,
        mapping_id=prepared.mapping_id,
        session_ref=prepared.session_ref,
        scope=prepared._scope,
    )
    assert persistence.scope == port["scope"]
    assert persistence.binding.native_refs["session"] == prepared.ma_session_id


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("stage", ["preparation", "callback"])
async def test_worker_expiry_attempts_cleanup_of_the_actual_prepared_session_once(
    explicit_worker_request, monkeypatch, stage, capsys
):
    from datetime import timedelta

    from daimon.core.errors import TurnError
    from daimon.core.mux_backend import TurnBackendRequest
    from daimon.core.turn import prepare as preparation
    from daimon.core.turn.io import turn_io
    from daimon.core.turn.run import _archive_orphaned_session

    request, _, _, transport = explicit_worker_request
    clock = [request.now()]
    request = replace(request, now=lambda: clock[0])
    bound = request.worker.limits.deadline
    profile = request.admission.backend_revision.profile
    assert preparation.ensure_channel_runtime(
        profile, ready=lambda: profile in preparation._PROVIDER_PREPARATIONS
    )
    prepare = preparation._PROVIDER_PREPARATIONS[profile]
    archived = []
    prepared_callbacks = []

    async def timed_prepare(selected):
        result = await prepare(selected)
        if stage == "preparation":
            clock[0] = bound + timedelta(seconds=1)
        return result

    async def on_prepared(result):
        prepared_callbacks.append(result)
        clock[0] = bound + timedelta(seconds=1)

    async def orphan(result):
        assert result._scope == request.worker.permissions.fleet.scope
        io = turn_io(
            request.deps.anthropic,
            result.ma_session_id,
            path="mux",
            backend=result.backend,
            scope=result._scope,
            session_ref=result.session_ref,
            profile=profile,
            backend_request=TurnBackendRequest(
                profile,
                request.deps.anthropic,
                result._scope,
                result.ma_session_id,
                deps=request.deps,
                config=request.admission.backend_revision,
                session=result.session_ref,
                runtime=result.runtime,
            ),
        )
        await _archive_orphaned_session(
            request.deps.anthropic, session_id=result.ma_session_id, io=io
        )
        archived.append(result)

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, timed_prepare)
    with pytest.raises(TurnError) as refused:
        await _bind_worker(request, _on_prepared=on_prepared, _on_orphan=orphan)
    assert refused.value.kind == "ceiling"
    assert len(archived) == 1 and len(prepared_callbacks) == (stage == "callback")
    session = await archived[0].backend.sessions.retrieve(
        archived[0]._scope, archived[0].session_ref
    )
    assert session.state == "idle"
    assert "session_archive" in capsys.readouterr().out
    assert not transport.requests


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_normalized_source_prefix_is_consumed_by_actual_preparation(
    explicit_worker_request, monkeypatch
):
    from daimon.core.neutral_resources import resolve_worker_spec
    from daimon.core.turn.runtime_manifest import capture_preparation_source

    from ..test_fleet_permissions import POLICY

    request, _, owned, transport = explicit_worker_request
    captured = await capture_preparation_source(request)
    root = replace(
        request.worker.permissions,
        fleet=replace(request.worker.permissions.fleet, depth=0),
        base_system="Protected root instruction",
    )
    worker = resolve_worker_spec(
        root,
        root.spec.model_copy(update={"system": root.base_system}),
        policy=POLICY,
        now=request.now(),
        captured_source=captured,
    )
    assert worker.spec.system.startswith("Retained instructions\n\nProtected root instruction\n\n")
    assert worker.spec.system.count(root.base_system) == 1
    scope = worker.permissions.fleet.scope
    actual = await owned.resources.backend.agents.create(
        scope, worker.spec, key="normalized-prefix"
    )
    previous = await owned.resources.catalog.get_agent(scope, owned.retained.catalog_id)
    assert previous is not None
    row = await owned.resources.catalog.put_agent(
        scope,
        owned.retained.catalog_id,
        actual,
        expected_revision=previous.local_revision,
    )
    plan = owned.plan.model_copy(
        update={
            "agent_id": actual.ref.id,
            "agent_name": worker.spec.name,
            "tools": tuple(worker.spec.tools or ()),
            "skill_ids": tuple(pin.id for pin in worker.spec.skills or ()),
            "system": worker.spec.system,
            "counterpart_catalog_revision": row.local_revision,
        }
    )
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    request, _ = install_runtime(
        replace(request, scope=scope, worker=worker), owned, monkeypatch, transport
    )
    selected = await read_preparation_counterpart(request)
    assert selected.counterpart.agent.spec == worker.spec
    assert selected.source.effective_spec.system == "Retained instructions"
