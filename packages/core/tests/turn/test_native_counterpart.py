"""Operator pins authorize owned readback, never incomplete source execution."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Literal, NamedTuple

import httpx
import pytest
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_deployment import GeminiPlan, build_gemini_runtime
from daimon.core.turn.openai_deployment import OpenAIDeployment, build_openai_runtime
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.testing import pytest_plugin as database_plugin
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.drivers.gemini.fake import MemoryStorage
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.isolated_sdk import SDK_ENV_VARS, build_async_openai, build_gemini_client
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope
from mux.contracts.resources import Agent, AgentSpec, Environment, SkillUpload, SkillUploadFile
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
from mux.drivers.gemini.transport import SDKTransport as GeminiSDKTransport
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.state.memory import MemoryStateStore
from pydantic import ValidationError

from .test_openai_deployment import application as application
from .test_provider_dispatch import ACCOUNT, REVISION, SCOPE, TENANT

SOURCE_ID = derive_agent_uuid(tenant_id=TENANT, ma_agent_id="host-metadata-not-a-provider-agent")
PINS = {
    "source_agent_id": str(SOURCE_ID),
    "source_agent_version": 1,
    "counterpart_catalog_id": "owned-counterpart",
    "counterpart_catalog_revision": 1,
}


@pytest.fixture(params=["openai", "gemini"])
def provider(request: pytest.FixtureRequest) -> Literal["openai", "gemini"]:
    return request.param


def deployment(provider: str = "openai", tmp_path: Path = Path("/offline")) -> dict[str, Any]:
    profile = "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse"
    model = "gpt-6-luna" if provider == "openai" else "gemini-3.8-flash"
    row = {
        "profile": profile,
        "tenant_id": SCOPE.tenant_id,
        "platform": "slack",
        "channel_id": "channel",
        "account_id": SCOPE.account_id,
        "configuration_grant": {
            "profiles": [profile],
            "models": [model],
            "thread_modes": ["per_caller"],
            "requires": {},
        },
        "project": "project",
        "agent_id": "owned-agent",
        "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
    }
    if provider == "openai":
        row["environment_id"] = "owned-environment"
    else:
        row.update(
            records_path=str(tmp_path / "records.sqlite"),
            holds_path=str(tmp_path / "holds.sqlite"),
            hold_usd="0.05",
            cap_usd="0.25",
            agent_name="unrelated-display-label",
            system="Retained instructions",
            tools=[],
            environment={"name": "inline"},
        )
    return row


def plan_type(provider: str) -> type[OpenAIDeployment] | type[GeminiPlan]:
    return OpenAIDeployment if provider == "openai" else GeminiPlan


@pytest.mark.parametrize(
    "names",
    [choice for size in (1, 2, 3) for choice in combinations(PINS, size)],
)
def test_partial_counterpart_pins_are_refused(names: tuple[str, ...], provider: str) -> None:
    with pytest.raises(ValidationError, match="complete source and catalog revision pins"):
        plan_type(provider).model_validate(
            {**deployment(provider), **{key: PINS[key] for key in names}}
        )


@pytest.mark.parametrize("key", ["source_agent_version", "counterpart_catalog_revision"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
def test_counterpart_revision_is_a_positive_strict_integer(
    key: str, value: object, provider: str
) -> None:
    with pytest.raises(ValidationError):
        plan_type(provider).model_validate({**deployment(provider), **PINS, key: value})


def test_existing_channel_deployment_has_no_implicit_counterpart(provider: str) -> None:
    row = plan_type(provider).model_validate(deployment(provider))
    assert row.source_agent_id is None
    assert row.source_agent_version is None
    assert row.counterpart_catalog_id is None
    assert row.counterpart_catalog_revision is None


def test_complete_operator_pins_remain_separate_from_configuration_authority(provider: str) -> None:
    row = plan_type(provider).model_validate({**deployment(provider), **PINS})
    assert row.source_agent_id == SOURCE_ID
    assert row.source_agent_version == 1
    assert row.counterpart_catalog_revision == 1
    assert row.config_digest is None
    assert row.configuration_grant is not None
    with pytest.raises(ValidationError, match="configuration authorization"):
        plan_type(provider).model_validate(
            {**deployment(provider), **PINS, "configuration_grant": None}
        )


class Wire:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.requests: list[httpx.Request] = []
        self.agent: dict[str, Any] = {
            "id": "owned-agent",
            "name": "unrelated-display-label",
            "model": "gpt-6-luna",
            "instructions": "Retained instructions",
            "tools": [],
            "metadata": {"mux_tenant": str(TENANT)},
            "created_at": 1791792000,
        }
        self.environment: dict[str, Any] = {
            "id": "owned-environment",
            "name": "hosted",
            "files": [],
            "network": {"access": "disabled"},
            "created_at": 1791792000,
        }
        self.error: tuple[str, int] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.provider == "gemini":
            assert request.headers["Api-Revision"] == "2026-05-20"
            if request.method == "POST":
                assert request.url.path == "/v1beta/agents"
                self.agent = json.loads(request.content)
                self.native_id = self.agent["id"]
                return httpx.Response(200, json=self.agent)
            assert request.method == "GET"
            assert request.url.path == "/v1beta/agents/" + self.agent_id
            if self.error is not None:
                return httpx.Response(
                    self.error[1], json={"error": {"message": "private provider text"}}
                )
            return httpx.Response(200, json=self.agent)
        assert request.method == "GET", "counterpart readback cannot mutate native state"
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        path = request.url.path
        assert path in (
            "/v1/agents/owned-agent",
            "/v1/agents/environments/templates/owned-environment",
        )
        if self.error is not None and self.error[0] == path:
            return httpx.Response(
                self.error[1], json={"error": {"message": "private provider text"}}
            )
        raw = self.agent if path == "/v1/agents/owned-agent" else self.environment
        return httpx.Response(200, json=raw)

    @property
    def agent_id(self) -> str:
        return self.native_id


class Owned(NamedTuple):
    request: ProviderPreparationRequest
    source: BackendAgent
    plan: OpenAIDeployment | GeminiPlan
    resources: NeutralResourceHost
    retained: AgentRevision
    wire: Wire
    manifest_path: Path


db_preflight = database_plugin.db_preflight


@pytest.fixture(scope="session", autouse=True)
def owned_database_preflight(db_preflight: DatabasePreflight) -> None:
    """Admit every engine/bind before fixture construction can connect."""


@pytest.fixture(autouse=True)
def clean_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SDK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def owned(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> AsyncIterator[Owned]:
    require_owned_sessionmaker(application.deps.sessionmaker)
    async with application.deps.sessionmaker.begin() as db:
        tenant = await make_tenant(db, id=TENANT)
        await make_account(db, id=ACCOUNT, tenant=tenant)
    selected = replace(application.admission, backend_revision_origin="agent")
    assert selected.agent.version == 1
    request = replace(application, admission=selected)
    if provider == "gemini":
        config = ConfigRevision.create(
            REVISION.channel,
            1,
            resolve_default(
                BackendConfig(
                    backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
                )
            ),
        )
        request = replace(
            request,
            admission=replace(request.admission, backend_revision=config),
            deps=replace(request.deps, state_store=MemoryStateStore()),
        )
    source = BackendAgent(
        tenant_id=TENANT,
        agent_id=SOURCE_ID,
        name="source-display-name",
        configuration_digest="trusted-owner-source-projection",
    )
    wire = Wire(provider)
    async with AsyncExitStack() as stack:
        if provider == "openai":
            sdk = await stack.enter_async_context(
                build_async_openai(
                    transport=httpx.MockTransport(wire.handle),
                    api_key="offline",
                    base_url="https://openai.invalid/v1",
                )
            )
            backend = OpenAIDriver(
                SDKTransport(sdk),
                account_scope_id="project",
                journal=MemoryRecoveryJournal(),
                usage_revisions=MemoryUsageRevisions(),
                authorization=lambda scope, kind, id_: (
                    scope == SCOPE
                    and (kind, id_)
                    in {("agent", "owned-agent"), ("environment", "owned-environment")}
                ),
            )
        else:
            client = build_gemini_client(
                transport=httpx.MockTransport(wire.handle),
                api_key="offline",
                base_url="https://gemini.invalid",
            )
            stack.callback(client.close)
            stack.push_async_callback(client.aio.aclose)
            backend = GeminiManagedAgents(
                GeminiSDKTransport(client),
                storage=MemoryStorage(),
                state_store=MemoryStateStore(),
                account_scope_id="project",
                resource_catalog_enabled=True,
                resource_catalog_transport=SDKAgentCatalogTransport(client),
            )
        resources = NeutralResourceHost(
            backend,
            request.deps.sessionmaker,
            scope=SCOPE,
            account_scope_id="project",
            operation_key="owned-counterpart-read",
            resource_catalog_enabled=provider == "gemini",
            catalog_adapter=getattr(backend, "catalog_adapter", None),
        )
        spec = AgentSpec(
            name="unrelated-display-label",
            model=ModelRef(
                provider=provider, id="gpt-6-luna" if provider == "openai" else "gemini-3.8-flash"
            ),
            system="Retained instructions",
            tools=(),
            mcp_servers=(),
            skills=None,
        )
        if provider == "openai":
            agent = Agent(
                ref=ResourceRef(
                    id="owned-agent",
                    kind="agent",
                    provider="openai",
                    account_scope_id="project",
                    tenant_id=SCOPE.tenant_id,
                    account_id=SCOPE.account_id,
                ),
                revision=Revision(local=0),
                spec=spec,
                created_at=datetime(2026, 10, 12, tzinfo=UTC),
            )
        else:
            skill = await backend.skills.create(
                SCOPE,
                SkillUpload(
                    files=(
                        SkillUploadFile(path="SKILL.md", content=b"# Retained\nUse every file.\n"),
                        SkillUploadFile(path="scripts/check.sh", content=b"true\n"),
                    )
                ),
                key="concrete-counterpart-skill",
            )
            assert skill.latest_version is not None
            spec = spec.model_copy(update={"skills": (skill.latest_version,)})
            agent = await backend.agents.create(SCOPE, spec, key="counterpart-fixture")
        wire.native_id = agent.ref.id
        wire.requests.clear()
        retained = await resources.catalog.put_agent(
            SCOPE, "owned-counterpart", agent, expected_revision=0
        )
        plan = plan_type(provider).model_validate(
            {
                **deployment(provider, tmp_path),
                **PINS,
                "agent_id": agent.ref.id,
                "skill_ids": [pin.id for pin in spec.skills or ()],
            }
        )
        manifest_path = tmp_path / "operator-runtime.json"
        manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
        monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest_path))
        original = os.environ.get

        def refuse_key(name: str, default: str | None = None) -> str | None:
            if name == plan.api_key_env:
                raise AssertionError("native readback component discovered a credential")
            return original(name, default)

        monkeypatch.setattr(os.environ, "get", refuse_key)
        yield Owned(request, source, plan, resources, retained, wire, manifest_path)


async def test_owned_counterpart_reads_actual_native_resources_and_returns_retained_revision(
    owned: Owned,
) -> None:
    row = await owned.plan.read_counterpart(
        owned.request, source=owned.source, resources=owned.resources
    )
    assert row == owned.retained
    assert [r.url.path for r in owned.wire.requests] == (
        ["/v1/agents/owned-agent", "/v1/agents/environments/templates/owned-environment"]
        if owned.wire.provider == "openai"
        else ["/v1beta/agents/" + owned.plan.agent_id]
    )
    assert row.agent.ref.id != owned.request.admission.agent.id
    assert row.agent.spec.name != owned.source.name, "names never establish counterpart authority"
    assert row.agent.revision == owned.retained.agent.revision, (
        "readback cannot invent a native CAS"
    )


@pytest.mark.parametrize(
    "defect",
    [
        "source_id",
        "source_tenant",
        "source_version",
        "recreated_same_name",
        "tenant",
        "account",
        "project",
        "channel",
        "platform",
        "origin",
        "unset_origin",
        "disabled",
        "legacy",
        "catalog_id",
        "catalog_revision",
        "native_id",
        "skill_id",
        "profile",
        "model",
        "thread_mode",
        "requires",
        "exact_digest",
        "scope",
        "session_account",
    ],
)
async def test_source_bounds_and_catalog_mismatches_refuse_before_native_io(
    owned: Owned, defect: str
) -> None:
    request, source, plan, resources, _, wire, manifest_path = owned
    values = owned.plan.model_dump(mode="json")
    if defect in ("tenant", "account", "project", "channel", "platform", "native_id"):
        values[
            {
                "tenant": "tenant_id",
                "account": "account_id",
                "project": "project",
                "channel": "channel_id",
                "platform": "platform",
                "native_id": "agent_id",
            }[defect]
        ] = "foreign"
    elif defect == "source_id":
        values["source_agent_id"] = str(uuid.UUID(int=777))
    elif defect == "source_version":
        values["source_agent_version"] = 2
    elif defect == "source_tenant":
        source = source.model_copy(update={"tenant_id": uuid.UUID(int=777)})
    elif defect == "recreated_same_name":
        request = replace(
            request,
            admission=replace(
                request.admission,
                agent=request.admission.agent.model_copy(update={"id": "recreated-source"}),
            ),
        )
    elif defect in ("catalog_id", "catalog_revision", "skill_id"):
        values[
            {
                "catalog_id": "counterpart_catalog_id",
                "catalog_revision": "counterpart_catalog_revision",
                "skill_id": "skill_ids",
            }[defect]
        ] = {"catalog_id": "missing", "catalog_revision": 2, "skill_id": ["foreign-skill"]}[defect]
    elif defect == "origin":
        request = replace(
            request, admission=replace(request.admission, backend_revision_origin="channel")
        )
    elif defect == "unset_origin":
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            replace(request.admission, backend_revision_origin=None)
        damaged = replace(request.admission)
        object.__setattr__(damaged, "backend_revision_origin", None)
        request = replace(request, admission=damaged)
    elif defect in ("disabled", "legacy"):
        request = replace(
            request,
            deps=replace(request.deps, channel_backends=False)
            if defect == "disabled"
            else replace(request.deps, turn_path="legacy"),
        )
    elif defect == "scope":
        request = replace(
            request, scope=SCOPE.model_copy(update={"authorization_id": "other-authority"})
        )
    elif defect == "session_account":
        request = replace(request, session_account_id=uuid.UUID(int=777))
    elif defect == "exact_digest":
        values.update(configuration_grant=None, config_digest="caller-supplied-digest")
    else:
        selection: dict[str, Any] = {
            "backend": wire.provider,
            "profile": plan.profile,
            "model": request.admission.backend_revision.model,
        }
        selection.update(
            {
                "profile": {"backend": "openai", "profile": "openai.conversation_only"},
                "model": {"model": "gpt-6-astra"},
                "thread_mode": {"thread_mode": "shared"},
                "requires": {"requires": {"artifacts": {"level": "required"}}},
            }[defect]
        )
        revision = ConfigRevision.create(
            REVISION.channel, 2, resolve_default(BackendConfig.model_validate(selection))
        )
        request = replace(request, admission=replace(request.admission, backend_revision=revision))
    plan = type(plan).model_validate(values)
    manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    with pytest.raises(AdmissionDenied) as error:
        await plan.read_counterpart(request, source=source, resources=resources)
    assert error.value.reason == "backend_unsupported"
    assert wire.requests == []


@pytest.mark.parametrize(
    "defect",
    [
        "instructions",
        "model",
        "tool",
        "wrong_agent",
        "wrong_environment",
        "agent_deleted",
        "environment_deleted",
        "malformed_agent",
        "malformed_environment",
        "foreign_native_tenant",
    ],
)
async def test_native_mutations_and_absence_cannot_certify_counterpart(
    owned: Owned, defect: str
) -> None:
    if owned.wire.provider == "gemini":
        field = {
            "instructions": "system_instruction",
            "model": "agent_config",
            "tool": "tools",
            "wrong_agent": "id",
            "wrong_environment": "base_environment",
            "malformed_agent": "base_agent",
            "malformed_environment": "base_environment",
            "foreign_native_tenant": "id",
        }.get(defect)
        if defect in ("agent_deleted", "environment_deleted"):
            owned.wire.error = ("native", 404)
        elif defect.startswith("malformed"):
            del owned.wire.agent[field]
        else:
            owned.wire.agent[field] = "foreign"
    else:
        if defect == "instructions":
            owned.wire.agent["instructions"] = "changed"
        elif defect == "model":
            owned.wire.agent["model"] = "gpt-6-astra"
        elif defect == "tool":
            owned.wire.agent["tools"] = [{"type": "web_search"}]
        elif defect == "wrong_agent":
            owned.wire.agent["id"] = "foreign"
        elif defect == "wrong_environment":
            owned.wire.environment["id"] = "foreign"
        elif defect == "malformed_agent":
            del owned.wire.agent["model"]
        elif defect == "malformed_environment":
            del owned.wire.environment["created_at"]
        elif defect == "foreign_native_tenant":
            owned.wire.agent["metadata"]["mux_tenant"] = "foreign"
        else:
            owned.wire.error = (
                "/v1/agents/owned-agent"
                if defect == "agent_deleted"
                else "/v1/agents/environments/templates/owned-environment",
                404,
            )
    with pytest.raises(AdmissionDenied) as error:
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )
    assert "private provider text" not in str(error.value)
    assert all(r.method == "GET" for r in owned.wire.requests)


async def test_catalog_change_during_readback_is_refused(
    owned: Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = owned.resources.read

    async def change(catalog_id: str) -> AgentRevision:
        await owned.resources.catalog.put_agent(
            SCOPE, owned.retained.catalog_id, owned.retained.agent, expected_revision=1
        )
        return await original(catalog_id)

    monkeypatch.setattr(owned.resources, "read", change)
    with pytest.raises(AdmissionDenied):
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )


async def test_caller_cannot_substitute_an_uninstalled_operator_grant(owned: Owned) -> None:
    values = owned.plan.model_dump(mode="json")
    values.update(
        config_digest=owned.request.admission.backend_revision.digest, configuration_grant=None
    )
    forged = type(owned.plan).model_validate(values)
    with pytest.raises(AdmissionDenied):
        await forged.read_counterpart(owned.request, source=owned.source, resources=owned.resources)
    assert owned.wire.requests == []


async def test_operator_revocation_during_readback_is_refused(
    owned: Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = owned.resources.read

    async def revoke(catalog_id: str) -> AgentRevision:
        owned.manifest_path.write_text("[]")
        return await original(catalog_id)

    monkeypatch.setattr(owned.resources, "read", revoke)
    with pytest.raises(AdmissionDenied):
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )


async def test_complete_native_component_still_cannot_unlock_agent_origin_execution(
    owned: Owned, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )
        == owned.retained
    )
    path = tmp_path / "operator-runtime.json"
    path.write_text(json.dumps([owned.plan.model_dump(mode="json")]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    before = len(owned.wire.requests)
    with pytest.raises(AdmissionDenied):
        (build_openai_runtime if owned.wire.provider == "openai" else build_gemini_runtime)(
            owned.request
        )
    assert len(owned.wire.requests) == before


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_gemini_native_readback_retains_every_concrete_skill_file(owned: Owned) -> None:
    row = await owned.plan.read_counterpart(
        owned.request, source=owned.source, resources=owned.resources
    )
    pins = row.agent.spec.skills
    assert pins and pins[0].version not in (None, "latest")
    assert pins[0].digest == pins[0].version
    sources = owned.wire.agent["base_environment"]["sources"]
    assert {item["target"]: item["content"] for item in sources} == {
        f".agents/skills/{pins[0].id}/SKILL.md": "# Retained\nUse every file.\n",
        f".agents/skills/{pins[0].id}/scripts/check.sh": "true\n",
    }


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("defect", ["system", "tools", "environment", "catalog_disabled"])
async def test_gemini_wrapper_requires_actual_native_catalog_policy(
    owned: Owned,
    defect: str,
) -> None:
    values = owned.plan.model_dump(mode="json")
    if defect == "system":
        values["system"] = "Different operator intent"
    elif defect == "tools":
        values["tools"] = [{"name": "google_search", "kind": "builtin"}]
    elif defect == "environment":
        values["environment"] = {
            "name": "unproven",
            "network": {"mode": "limited", "allowed_hosts": ["example.invalid"]},
        }
    else:
        owned.resources.backend.agents.enabled = False
    plan = GeminiPlan.model_validate(values)
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    with pytest.raises(AdmissionDenied) as error:
        await plan.read_counterpart(owned.request, source=owned.source, resources=owned.resources)
    assert error.value.reason == "backend_unsupported"


def test_gemini_complete_pins_require_explicit_native_identity() -> None:
    with pytest.raises(ValidationError, match="native agent identity"):
        GeminiPlan.model_validate({**deployment("gemini"), **PINS, "agent_id": None})


@pytest.mark.parametrize("provider", ["openai"], indirect=True)
@pytest.mark.parametrize("change", ["catalog", "manifest"])
async def test_openai_authority_change_during_environment_get_is_refused(
    owned: Owned,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    original = owned.resources.backend.environments.retrieve

    async def mutate(scope: Scope, ref: ResourceRef) -> Environment:
        if change == "catalog":
            await owned.resources.catalog.put_agent(
                scope,
                owned.retained.catalog_id,
                owned.retained.agent,
                expected_revision=1,
            )
        else:
            owned.manifest_path.write_text("[]")
        return await original(scope, ref)

    monkeypatch.setattr(owned.resources.backend.environments, "retrieve", mutate)
    with pytest.raises(AdmissionDenied):
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )
    assert [r.url.path for r in owned.wire.requests] == [
        "/v1/agents/owned-agent",
        "/v1/agents/environments/templates/owned-environment",
    ]
