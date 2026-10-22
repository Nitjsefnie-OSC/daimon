"""A Gemini agent template keeps Daimon's MCP tools with a per-turn in-memory credential.

A template row ("root_materialization" + "template_capabilities": "mcp") has no
operator capability inputs: capture represents the source's daimon-mcp toolset
from its live catalogue, consent keeps it (skills, memory and sandbox stay
declared drops), the host publishes the counterpart once, and each turn mints
one registered caller credential in memory that `finalize` revokes. Only the
HTTP edge is replaced; the MCP catalogue is the real verifier and middleware.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from daimon.core.mcp_auth import token_jti
from daimon.core.stores import mux_state
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_deployment import GeminiPlan
from daimon.core.turn.prepare import admitted_session_scope
from daimon.core.turn.runtime_manifest import read_preparation_counterpart
from daimon.core.turn.runtimes import build_channel_runtime, scope_channel_runtimes
from mux.contracts.config import ConfigRevision
from mux.contracts.resources import EnvironmentSpec
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.errors import ScopeViolation

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_gemini_codec import interaction
from .test_gemini_full_source import PARAMS
from .test_gemini_full_source import source_mcp as source_mcp
from .test_gemini_runtime_transport import clients_for


@pytest.fixture
async def template(consent_request, source_mcp, monkeypatch, tmp_path):
    from daimon.core.authz import Place
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.scope import DeploymentDefault, ScopeContext
    from daimon.core.turn.admission import capture_backend_source
    from daimon.testing.asgi import mcp_session

    request, service, source, actor, accepted, router, *_ = consent_request
    sessions = request.deps.sessionmaker
    router.add_environment_list(request.admission.environment)
    scope = admitted_session_scope(
        request.admission, tenant_id=request.tenant_id, session_id="preparation"
    )
    request = replace(
        request,
        scope=scope,
        admission=replace(
            request.admission,
            origin_channel_id=request.admission.backend_revision.channel.channel_id,
        ),
        now=lambda: datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        deps=replace(
            request.deps,
            deployment_default=DeploymentDefault(
                agent_name=source.name, environment_name="test-env"
            ),
            turn_runtimes={},
            turn_runtime_factory=build_channel_runtime,
            state_store=PostgresStateStore(sessions),
        ),
    )
    native, calls, posts = {}, [], []

    def handle(call):
        calls.append(call)
        if call.url.path.startswith("/v1beta/agents"):
            if call.method == "POST":
                native.update(json.loads(call.content))
            return httpx.Response(200, json=native)
        if call.method == "POST":
            posts.append(json.loads(call.content))
            return httpx.Response(200, json=interaction(f"turn-{len(posts)}"))
        return httpx.Response(200, json=interaction(call.url.path.rsplit("/", 1)[-1]))

    async def catalogue(call):
        assert call.url.host == "mcp.invalid"
        body = json.loads(call.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result: dict[str, object] = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "offline", "version": "1"},
        }
        if body["method"] == "tools/list":
            token = call.headers["Authorization"].removeprefix("Bearer ")
            result = (await mcp_session(source_mcp, token=token, method="tools/list"))["result"]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real = httpx.AsyncClient  # captured before the SDK edge is replaced
    clients = clients_for(monkeypatch, handle)
    sdk_client = httpx.AsyncClient

    class RouteClient(sdk_client):
        def __init__(self, **kwargs):
            if "verify" in kwargs and "transport" not in kwargs:
                # Preserve the real SDK's explicit network-policy assertion.
                super().__init__(**kwargs)
            else:
                kwargs.setdefault("transport", httpx.MockTransport(catalogue))
                real.__init__(self, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RouteClient)
    revision = request.admission.backend_revision
    plan = GeminiPlan(
        profile=revision.profile,
        tenant_id=scope.tenant_id,
        platform="*",
        channel_id="*",
        account_id="*",
        configuration_grant={
            "profiles": ["gemini.inline_reuse"],
            "models": [FLASH_PRIMARY],
            "thread_modes": ["per_caller"],
            "requires": {},
        },
        project="project",
        api_key_env="OFFLINE_TEMPLATE_GEMINI_KEY",
        records_path=tmp_path / "records.sqlite",
        holds_path=tmp_path / "holds.sqlite",
        hold_usd="0.05",
        cap_usd="0.25",
        agent_name="template-label",
        tools=(),
        environment=EnvironmentSpec(name="remote"),
        root_materialization=True,
        template_capabilities="mcp",
    )
    manifest = tmp_path / "runtime.json"
    manifest.write_text(json.dumps([plan.model_dump(mode="json")]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    monkeypatch.setenv(plan.api_key_env, "offline-template")

    async def capture_bound_source(actual_source):
        return await capture_backend_source(
            request.deps,
            actual_source,
            context=ScopeContext(
                tenant_id=request.tenant_id,
                account_id=request.admission.account_id,
                platform=request.platform,
                channel_id=request.admission.origin_channel_id,
                thread_id=request.thread_id,
            ),
            subject=actor.subject,
            place=Place(
                channel_id=request.thread_id, parent_channel_id=request.admission.origin_channel_id
            ),
            is_dm=False,
            slack_turn_context_id=None,
            private_dm_id=None,
            external_user_id=request.external_user_id,
        )

    consenting = AgentBackendService(sessions, enabled=True, capture_source=capture_bound_source)
    quote = await consenting.preview(source, "gemini")
    confirmed = await consenting.set_backend(
        source,
        "gemini",
        accepted.local,
        actor,
        accepted_drops=quote.declared_drops,
        expected_preview_digest=quote.digest,
    )
    linked = ConfigRevision.create(revision.channel, 2, revision)
    async with sessions.begin() as db:
        await mux_state.put_config_revision(db, linked, origin="agent", agent_revision=confirmed)
    request = replace(request, admission=replace(request.admission, backend_revision=linked))
    calls.clear()
    yield request, quote, native, calls, posts
    assert all(client.is_closed for client in clients)


async def _prepared(request):
    scoped, lookup = scope_channel_runtimes(request)
    selected = await read_preparation_counterpart(scoped)
    return selected, lookup.resolved


def _connection(selected):
    (connection,) = selected.target_spec.mcp_servers
    return connection


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_consent_keeps_daimon_tools_and_drops_only_what_gemini_lacks(template):
    _, quote, *_ = template
    assert "mcp" not in quote.declared_drops and "tools" in quote.declared_drops
    assert {"memory", "skills"} <= set(quote.declared_drops)


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_a_template_turn_mints_one_registered_credential_and_revokes_it(template):
    request, _, native, calls, _ = template
    selected, runtime = await _prepared(request)
    assert native["tools"] and native["tools"][-1]["type"] == "mcp_server"
    assert "headers" not in native["tools"][-1], "the stored agent holds no credential"
    connection = _connection(selected)
    transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
    token = await transport.mcp_secrets(request.scope, connection.credential_ref, connection.url)
    assert (
        await transport.mcp_secrets(request.scope, connection.credential_ref, connection.url)
        == token
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    # The requesting person's account, the agent's chat-execution grant, never admin.
    assert claims["sub"] == request.scope.account_id
    assert "is_admin" not in claims and "internal" not in claims
    async with request.deps.sessionmaker() as db:
        row = await get_mcp_token(db, jti=token_jti(token))
    assert row is not None and row.revoked_at is None
    assert str(row.account_id) == request.scope.account_id
    assert row.expires_at <= datetime.now(UTC) + timedelta(seconds=30 + 60 + 5)
    assert row.expires_at <= datetime.now(UTC) + timedelta(seconds=900)
    await runtime.end_of_turn()
    async with request.deps.sessionmaker() as db:
        revoked = await get_mcp_token(db, jti=token_jti(token))
    assert revoked is not None and revoked.revoked_at is not None
    await runtime.end_of_turn()  # idempotent once nothing is minted


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_the_credential_refuses_another_scope_reference_or_destination(template):
    request, *_ = template
    selected, runtime = await _prepared(request)
    connection = _connection(selected)
    transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
    other = request.scope.model_copy(update={"account_id": "00000000-0000-0000-0000-0000000000ff"})
    with pytest.raises(ScopeViolation):
        await transport.mcp_secrets(other, connection.credential_ref, connection.url)
    with pytest.raises(ScopeViolation):
        await transport.mcp_secrets(request.scope, "caller-mcp:other", connection.url)
    with pytest.raises(ScopeViolation):
        await transport.mcp_secrets(
            request.scope, connection.credential_ref, "https://evil.invalid"
        )


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_a_catalogue_that_changed_since_consent_revokes_and_refuses(template, monkeypatch):
    from daimon.core.mcp_oauth import discovery

    request, *_ = template
    selected, runtime = await _prepared(request)
    connection = _connection(selected)

    async def widened(_url, _token):
        return frozenset({"search_tools", "call_tool", "admin_only_tool"})

    monkeypatch.setattr(discovery, "list_mcp_tools", widened)
    transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
    async with request.deps.sessionmaker() as db:
        before = {r.jti for r in await _tokens(db, request)}
    with pytest.raises(ScopeViolation):
        await transport.mcp_secrets(request.scope, connection.credential_ref, connection.url)
    async with request.deps.sessionmaker() as db:
        minted = [r for r in await _tokens(db, request) if r.jti not in before]
    assert minted and all(r.revoked_at is not None for r in minted), "refused credential revoked"


async def _tokens(db, request):
    from daimon.core._models import McpToken
    from sqlalchemy import select

    return (
        await db.scalars(
            select(McpToken).where(McpToken.account_id == request.admission.account_id)
        )
    ).all()


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_a_template_without_mcp_capability_still_drops_tools(template, tmp_path):
    request, *_ = template
    manifest = tmp_path / "runtime.json"
    (row,) = json.loads(manifest.read_text())
    row.pop("template_capabilities")
    manifest.write_text(json.dumps([row]))
    with pytest.raises(AdmissionDenied):
        # The consent kept MCP; a row that can't represent it refuses the turn.
        await _prepared(request)


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("outcome", ["answered", "provider_failed"])
async def test_a_real_turn_revokes_its_credential_however_it_ends(template, monkeypatch, outcome):
    import asyncio

    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.testing.turn_fakes import RecordingLifecycle

    request, _, native, calls, posts = template
    await _prepared(request)
    prepared = await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
    )
    async with request.deps.sessionmaker() as db:
        before = {r.jti for r in await _tokens(db, request)}
    if outcome == "provider_failed":
        original = httpx.MockTransport.handle_async_request

        async def failing(self, call):
            if call.method == "POST" and "interactions" in call.url.path:
                return httpx.Response(
                    400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT"}}
                )
            return await original(self, call)

        monkeypatch.setattr(httpx.MockTransport, "handle_async_request", failing)

    async def no_recovery():
        pytest.fail("the template turn changed its admitted source/session")

    try:
        await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            external_user_id=request.external_user_id,
            user_message="Use a Daimon tool",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            operation_key=f"template-mcp:{outcome}",
        )
    except Exception:
        assert outcome == "provider_failed"
    async with request.deps.sessionmaker() as db:
        minted = [r for r in await _tokens(db, request) if r.jti not in before]
    assert minted, "the turn dispatched a credential to the provider"
    assert all(r.revoked_at is not None for r in minted), "revoked when the turn ended"
    sent = [
        json.loads(c.content) for c in calls if c.method == "POST" and "interactions" in c.url.path
    ]
    if outcome == "answered":
        assert any(
            t.get("type") == "mcp_server" and "headers" in t
            for body in sent
            for t in body.get("tools", [])
        ), "the credential travels only in the MCP server entry"
