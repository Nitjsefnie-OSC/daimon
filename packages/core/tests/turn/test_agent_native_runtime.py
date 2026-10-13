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
    row = await read_preparation_counterpart(request)
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
    if failure in ("native_drift", "native_missing"):
        assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    else:
        assert not owned.wire.requests


async def test_real_host_refuses_complete_native_component_as_source_execution_proof(
    native_request, owned
):
    request, runtime, wire, posts = native_request
    row = await read_preparation_counterpart(request)
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
