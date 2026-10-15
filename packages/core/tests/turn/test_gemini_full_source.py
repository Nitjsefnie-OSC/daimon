"""Full default source survives consent, native ACK/readback and ordinary turns.

All SDK wire calls are intercepted. GCS receipts represent separately owned
provisioning; these tests perform no GCS or provider writes.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.stores import mux_state
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_deployment import GeminiPlan, build_gemini_runtime
from daimon.core.turn.gemini_state import SQLiteGeminiStorage
from daimon.core.turn.prepare import admitted_session_scope, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtime_manifest import (
    capture_preparation_source,
    prepare_source,
    read_preparation_counterpart,
    source_preservation_pin,
)
from daimon.core.turn.runtimes import build_channel_runtime, scope_channel_runtimes
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, SkillRef
from mux.contracts.resources import EnvironmentSpec, SkillUpload, SkillUploadFile
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.bundles import SKILL_SOURCE, validate_bundle
from mux.drivers.gemini.deployment import configured_catalog_transport, configured_transport
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.drivers.gemini.skill_deployment import (
    GCSBundleDeployment,
    RetainedSkillDeployments,
    file_manifest,
)
from mux.errors import ScopeViolation, UnsupportedCapability

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_gemini_codec import interaction
from .test_gemini_runtime_transport import clients_for

ROOT = Path(__file__).resolve().parents[4]
DEFAULT = yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
PARAMS = {
    "backend": "gemini",
    "caller_mcp": True,
    "tools": [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {"name": t["name"], "enabled": True, "permission_policy": {"type": "always_allow"}}
                for t in DEFAULT["tools"][0]["configs"]
            ],
        }
    ],
    "skills": [
        {"type": "custom", "skill_id": f"source-{s['skill_id']}", "version": "1"}
        for s in DEFAULT["skills"]
    ],
}


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as packed:
        for path, content in files.items():
            packed.writestr(f"retained/{path}", content)
    return stream.getvalue()


@pytest.fixture
async def full(consent_request, monkeypatch, tmp_path):
    request, service, source, actor, accepted, router, source_reads, *_ = consent_request
    sessions = request.deps.sessionmaker
    scope = admitted_session_scope(
        request.admission, tenant_id=request.tenant_id, session_id="preparation"
    )
    request = replace(
        request,
        scope=scope,
        now=lambda: datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        deps=replace(
            request.deps,
            turn_runtimes={},
            turn_runtime_factory=build_channel_runtime,
            state_store=PostgresStateStore(sessions),
        ),
    )
    bundles, archives, deployments = [], [], []
    for item in DEFAULT["skills"]:
        name = item["skill_id"]
        directory = ROOT / "defaults/skills" / name
        files = {
            p.relative_to(directory).as_posix(): p.read_bytes()
            for p in sorted(directory.rglob("*"))
            if p.is_file()
        }
        bundle = SkillUpload(
            files=tuple(SkillUploadFile(path=p, content=b) for p, b in files.items())
        )
        version = validate_bundle(bundle, external=True)
        bundles.append(bundle)
        data = archive(files)
        path = tmp_path / f"{name}.zip"
        path.write_bytes(data)
        archives.append(path)
        skill_id = f"source-{name}"
        router.add(
            "GET",
            rf"/v1/skills/{skill_id}/versions",
            lambda *_, sid=skill_id: httpx.Response(
                200, json={"data": [{"id": f"version-{sid}", "version": "1"}], "has_more": False}
            ),
        )
        router.add(
            "GET",
            rf"/v1/skills/{skill_id}/versions/version-{skill_id}/content",
            lambda *_, body=data: httpx.Response(200, content=body),
        )
        deployments.append(
            GCSBundleDeployment(
                scope.tenant_id,
                scope.account_id,
                "project",
                version,
                f"gs://owned-offline/{scope.tenant_id}/{scope.account_id}/{version}/",
                file_manifest(bundle),
            )
        )
    native, calls, posts = {}, [], []

    def handle(call):
        calls.append(call)
        if call.url.path.startswith("/v1beta/agents"):
            if call.method == "POST":
                native.update(json.loads(call.content))
            else:
                assert call.method == "GET" and call.url.path.endswith(native["id"])
            return httpx.Response(200, json=native)
        if call.method == "POST":
            posts.append(json.loads(call.content))
            return httpx.Response(200, json=interaction(f"turn-{len(posts)}"))
        return httpx.Response(200, json=interaction(call.url.path.rsplit("/", 1)[-1]))

    clients = clients_for(monkeypatch, handle)

    async def never_resolve(*_):
        pytest.fail("Catalog persistence must never resolve a bearer")

    backend = GeminiManagedAgents(
        configured_transport(api_key="offline-full-source"),
        storage=SQLiteGeminiStorage(
            tmp_path / "records.sqlite",
            namespace=json.dumps(
                [scope.tenant_id, scope.account_id, "project"], separators=(",", ":")
            ),
        ),
        state_store=request.deps.state_store,
        account_scope_id="project",
        resource_catalog_enabled=True,
        resource_catalog_transport=configured_catalog_transport(api_key="offline-full-source"),
        mcp_secrets=never_resolve,
        skill_bundle_publisher=RetainedSkillDeployments(tuple(deployments)),
    )
    mappings = []
    for item, bundle, path in zip(DEFAULT["skills"], bundles, archives, strict=True):
        skill = await backend.skills.create(scope, bundle, key=item["skill_id"])
        version = skill.latest_version.version
        mappings.append(
            {
                "source": {"id": f"source-{item['skill_id']}", "source": "custom", "version": "1"},
                "target": SkillRef(
                    id=skill.id, source=SKILL_SOURCE, version=version, digest=version
                ).model_dump(mode="json"),
                "archive": str(path),
            }
        )
    manifest = tmp_path / "runtime.json"
    capabilities = {
        "source_pin": source_preservation_pin(source),
        "model": ModelRef(provider="gemini", id=FLASH_PRIMARY).model_dump(mode="json"),
        "skills": mappings,
        "mcp_tools": ["get_agent", "list_agents"],
    }
    manifest.write_text(
        json.dumps(
            [
                {
                    "tenant_id": scope.tenant_id,
                    "account_id": scope.account_id,
                    "profile": "gemini.inline_reuse",
                    "platform": request.platform,
                    "channel_id": request.admission.backend_revision.channel.channel_id,
                    "source_agent_id": str(source.agent_id),
                    "source_agent_version": source.source.revision.local,
                    "source_capabilities": capabilities,
                }
            ]
        )
    )
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    # This is the same fresh owner capture used by both preview and preparation.
    represented = await capture_preparation_source(request, fresh=True)
    assert source_preservation_pin(represented) == source_preservation_pin(source)
    quote = await service.preview(source, "gemini")
    assert quote.allowed and not {"tools", "skills", "mcp"}.intersection(quote.declared_drops)
    accepted = await service.set_backend(
        source,
        "gemini",
        accepted.local,
        actor,
        accepted_drops=quote.declared_drops,
        expected_preview_digest=quote.digest,
    )
    revision = ConfigRevision.create(
        request.admission.backend_revision.channel, 2, request.admission.backend_revision
    )
    async with sessions.begin() as db:
        await mux_state.put_config_revision(db, revision, origin="agent", agent_revision=accepted)
    request = replace(request, admission=replace(request.admission, backend_revision=revision))
    canonical = await prepare_source(request)
    host = NeutralResourceHost(
        backend,
        sessions,
        scope=scope,
        account_scope_id="project",
        operation_key="full-publication",
        resource_catalog_enabled=True,
        catalog_adapter=backend.catalog_adapter,
    )
    counterpart = await host.publish(
        "full-counterpart", canonical.target_spec, expected_revision=0, key="full-publication"
    )
    assert native and all("Bearer" not in json.dumps(body) for body in [native])
    # Native create/readback ACK precedes the existing token mint owner.
    async with sessions.begin() as db:
        token = await mint_agent_mcp_token(
            db,
            account_id=actor.account_id,
            tenant_id=source.tenant_id,
            agent_id=source.agent_id,
            label="offline-source",
            secret=request.deps.mcp.jwt_secret.get_secret_value().encode(),
            now=request.now(),
            ttl_days=600 / 86400,
        )
    credential = tmp_path / "caller.token"
    credential.write_text(token)
    credential.chmod(0o600)
    plan = GeminiPlan(
        profile=revision.profile,
        tenant_id=scope.tenant_id,
        platform=request.platform,
        channel_id=revision.channel.channel_id,
        account_id=scope.account_id,
        config_digest=revision.digest,
        project="project",
        api_key_env="OFFLINE_FULL_GEMINI_KEY",
        records_path=tmp_path / "records.sqlite",
        holds_path=tmp_path / "holds.sqlite",
        hold_usd="0.05",
        cap_usd="0.25",
        agent_name="operator-label",
        tools=canonical.target_spec.tools,
        environment=EnvironmentSpec(name="remote"),
        skill_ids=tuple(s.id for s in canonical.target_spec.skills),
        agent_id=counterpart.agent.ref.id,
        source_agent_id=source.agent_id,
        source_agent_version=source.source.revision.local,
        counterpart_catalog_id=counterpart.catalog_id,
        counterpart_catalog_revision=counterpart.local_revision,
        source_capabilities=capabilities,
        mcp_credential_file=credential,
        skill_deployments=tuple(deployments),
    )
    manifest.write_text(json.dumps([plan.model_dump(mode="json")]))
    monkeypatch.setenv(plan.api_key_env, "offline-full-source")
    calls.clear()
    yield request, plan, canonical, native, calls, posts, token, archives, source_reads, router
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_full_default_preparation_and_fresh_followup_turns(full):
    request, plan, canonical, native, calls, posts, token, *_ = full
    assert len(canonical.target_spec.skills) == 11
    assert len(native["base_environment"]["sources"]) == 11
    assert all(s["type"] == "gcs" for s in native["base_environment"]["sources"])
    selected = await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    assert selected.verified_counterpart == selected.counterpart
    assert all(call.method == "GET" for call in calls)
    session = None
    for index in range(2):
        prepared = await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=index != 0,
        )
        if session:
            assert prepared.ma_session_id == session
        session = prepared.ma_session_id

        async def no_recovery():
            pytest.fail("full source must not recover into a reduced backend")

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            external_user_id=request.external_user_id,
            user_message="Use the default skills and MCP.",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            operation_key=f"full:{index}",
        )
        assert outcome.state.error is None and not outcome.recovered
    assert len(posts) == 2
    assert posts[0]["environment"]["sources"] == native["base_environment"]["sources"]
    assert posts[1]["environment"] == "workspace"
    assert posts[1]["previous_interaction_id"] == "turn-1"
    for body in posts:
        assert body["agent"] == native["id"]
        assert body["system_instruction"] == canonical.target_spec.system
        assert token in json.dumps(body["tools"])
    assert token not in json.dumps(native) and token not in plan.model_dump_json()


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("defect", ["archive", "source_bytes", "native", "permissions", "revoked"])
async def test_full_source_drift_and_credential_failures_never_post(full, defect):
    request, plan, _, native, _, posts, token, archives, *_ = full
    if defect == "archive":
        archives[0].write_bytes(archive({"SKILL.md": b"changed"}))
    elif defect == "source_bytes":
        router = full[9]
        body = archive({"SKILL.md": b"source bytes changed under the same version"})
        router.routes.insert(
            0,
            (
                "GET",
                re.compile(r"/v1/skills/source-cli-auth/versions/version-source-cli-auth/content"),
                lambda *_: httpx.Response(200, content=body),
            ),
        )
    elif defect == "native":
        native["tools"] = []
    elif defect == "permissions":
        plan.mcp_credential_file.chmod(0o644)
    else:
        async with request.deps.sessionmaker.begin() as db:
            await revoke_mcp_token(db, jti=token_jti(token), now=request.now())
    if defect in ("archive", "source_bytes", "native"):
        with pytest.raises((AdmissionDenied, UnsupportedCapability)):
            await read_preparation_counterpart(scope_channel_runtimes(request)[0])
    else:
        prepared = await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=False,
        )

        async def no_recovery():
            pytest.fail("invalid auth must refuse")

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            external_user_id=request.external_user_id,
            user_message="Question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            operation_key="refusal",
        )
        assert outcome.state.error is not None
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
async def test_full_source_credential_cannot_resolve_before_native_verification(full):
    request, _, canonical, _, _, posts, token, *_ = full
    runtime = build_gemini_runtime(canonical)
    transport = runtime.transport_factory(request.admission.backend_revision, request.scope)
    connection = canonical.target_spec.mcp_servers[0]
    with pytest.raises(ScopeViolation, match="verified source binding"):
        await transport.mcp_secrets(request.scope, connection.credential_ref, connection.url)
    assert not posts


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("unusable", ["absent", "malformed"])
async def test_unrelated_manifest_failure_preserves_ordinary_source_capture(
    consent_request, monkeypatch, tmp_path, unusable
):
    request, _, source, *_ = consent_request
    path = tmp_path / "unusable.json"
    if unusable == "malformed":
        path.write_text("invalid JSON")
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    captured = await capture_preparation_source(request, fresh=True)
    assert captured == source
    assert source_preservation_pin(captured) == source_preservation_pin(source)
