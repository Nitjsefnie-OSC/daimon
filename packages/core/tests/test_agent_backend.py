"""Backend preferences use real policy, immutable revisions and scoped audit rows."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
from daimon.core.authz import Subject
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    BackendActor,
    BackendAgent,
    BackendUnsupported,
    CompatibilityItem,
    TenantBackendPolicy,
    check_backend,
    compatibility_preview,
    current_backend,
    make_backend_service,
    resolve_turn_backend,
    set_channel_backend,
)
from daimon.core.config import TurnSettings
from daimon.core.stores import mux_state, security_audit
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ChannelRef
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
async def owned(
    db_preflight: DatabasePreflight, db_nullpool_engine: AsyncEngine
) -> tuple[AgentBackendService, BackendAgent, BackendActor, async_sessionmaker[AsyncSession]]:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    agent = BackendAgent(
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        name="helper",
        configuration_digest="actual-config",
    )
    actor = BackendActor(tenant.id, account.id, Subject(is_admin=True))
    service = AgentBackendService(sessions, enabled=True)
    return service, agent, actor, sessions


async def enable(service: AgentBackendService, agent: BackendAgent, actor: BackendActor) -> None:
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(
        agent.tenant_id, ("anthropic", "openai", "gemini"), policy.revision, actor
    )


def test_preview_reports_unknown_and_unsupported_from_profile_metadata() -> None:
    agent = BackendAgent(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="helper",
        configuration_digest="actual",
        shared_threads=True,
        skills=(
            CompatibilityItem(
                name="native-pin", requires=("skills_bundle",), providers=("anthropic",)
            ),
        ),
        mcps=(CompatibilityItem(name="private-server", requires=("vaults",)),),
        required_capabilities=("native_schedules", "workspace_export_import"),
    )
    policy = TenantBackendPolicy(tenant_id=agent.tenant_id, revision="policy")
    preview = compatibility_preview(
        agent, "gemini", policy, cost_line="Existing list price; turn maximum unknown"
    )
    assert not preview.allowed
    assert preview.unsupported_skills == ("native-pin",)
    assert preview.unsupported_mcps == ("private-server",)
    assert preview.unsupported_capabilities == ("native_schedules", "workspace_export_import")
    assert preview.shared_thread_limits
    assert (
        preview.digest
        == compatibility_preview(agent, "gemini", policy, cost_line=preview.cost_line).digest
    )
    assert (
        preview.digest
        != compatibility_preview(
            agent.model_copy(update={"configuration_digest": "changed"}),
            "gemini",
            policy,
            cost_line=preview.cost_line,
        ).digest
    )
    assert (
        preview.digest
        != compatibility_preview(agent, "gemini", policy, cost_line="Changed rate").digest
    )
    assert (
        preview.digest
        != compatibility_preview(
            agent, "gemini", policy, setting_revision=1, cost_line=preview.cost_line
        ).digest
    )


async def test_default_and_disabled_resolution_perform_no_reads() -> None:
    class NoDatabase:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("disabled resolution accessed a database")

    channel = ChannelRef(tenant_id=str(uuid.uuid4()), platform="discord", channel_id="chan")
    assert await resolve_turn_backend(NoDatabase(), channel, "helper", enabled=False) == (
        None,
        False,
        "default",
    )
    assert await resolve_turn_backend(
        NoDatabase(), channel, "helper", enabled=True, retained_revision=0
    ) == (None, False, "default")


async def test_set_clear_revert_and_independent_audit_readback(owned) -> None:
    service, agent, actor, sessions = owned
    assert await service.current(agent) is None
    policy = await service.tenant_policy(agent.tenant_id)
    assert policy.enabled_backends == ("anthropic",)
    with pytest.raises(AgentBackendRefused, match="backend not allowed"):
        await service.set_backend(agent, "openai", 0, actor)
    await enable(service, agent, actor)
    preview = await service.preview(agent, "openai")
    first = await service.set_backend(
        agent, "openai", 0, actor, expected_preview_digest=preview.digest
    )
    second = await service.clear_backend(agent, 1, actor)
    third = await service.revert(agent, actor, expected_revision=2)
    assert [first.local, second.local, third.local] == [1, 2, 3]
    assert [first.backend, second.backend, third.backend] == ["openai", None, "openai"]
    assert await service.revision(agent, 1) == first
    async with sessions() as session:
        rows = await security_audit.list_events(session, tenant_id=agent.tenant_id)
    assert [r.operation for r in rows] == ["allowlist", "set", "clear", "revert"]
    assert [r.scope for r in rows[1:]] == [f"agent_backend:{agent.agent_id}:{n}" for n in (1, 2, 3)]
    assert all(r.account_id == actor.account_id for r in rows)


async def test_stale_preview_rejects_policy_agent_and_price_replacements(owned) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    preview = await service.preview(agent, "openai")
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await service.set_backend(
            agent.model_copy(update={"configuration_digest": "changed"}),
            "openai",
            0,
            actor,
            expected_preview_digest=preview.digest,
        )
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(
        agent.tenant_id, ("anthropic", "openai"), policy.revision, actor
    )
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await service.set_backend(agent, "openai", 0, actor, expected_preview_digest=preview.digest)
    assert await service.current(agent) is None
    line = {"value": "existing rate snapshot"}
    priced = AgentBackendService(sessions, enabled=True, cost_line=lambda a, b: line["value"])
    quote = await priced.preview(agent, "openai")
    line["value"] = "changed rate snapshot"
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await priced.set_backend(agent, "openai", 0, actor, expected_preview_digest=quote.digest)


@pytest.mark.parametrize(
    "subject",
    [
        Subject(),
        Subject(is_admin=True, via_agent_key=True),
        Subject(administered_channel_ids=frozenset({"chan"})),
    ],
)
async def test_non_admin_and_agent_key_refused_without_revision_or_audit(owned, subject) -> None:
    service, agent, actor, _ = owned
    with pytest.raises(AgentBackendRefused, match="admin required"):
        await service.set_backend(agent, "anthropic", 0, replace(actor, subject=subject))
    assert await service.current(agent) is None
    assert await service.audit(agent.tenant_id) == ()


async def test_foreign_tenant_and_account_refused(owned) -> None:
    service, agent, actor, sessions = owned
    async with sessions.begin() as session:
        foreign = await make_account(session)
    with pytest.raises(AgentBackendRefused, match="tenant mismatch"):
        await service.set_backend(
            agent, "anthropic", 0, replace(actor, tenant_id=foreign.tenant_id)
        )
    with pytest.raises(AgentBackendRefused, match="actor not found"):
        await service.set_backend(agent, "anthropic", 0, replace(actor, account_id=foreign.id))
    assert await service.current(agent) is None


async def test_concurrent_cas_has_one_winner_and_one_audit(owned) -> None:
    service, agent, actor, _ = owned
    results = await asyncio.gather(
        service.set_backend(agent, "anthropic", 0, actor),
        service.set_backend(agent, "anthropic", 0, actor),
        return_exceptions=True,
    )
    assert (
        sum(isinstance(r, AgentBackendRefused) and r.reason == "stale_revision" for r in results)
        == 1
    )
    assert (await service.current(agent)).local == 1
    assert len(await service.audit(agent.tenant_id)) == 1


async def test_audit_failure_rolls_back_setting(owned, monkeypatch) -> None:
    service, agent, actor, _ = owned

    async def unavailable(*args, **kwargs):
        raise RuntimeError("offline audit failure")

    monkeypatch.setattr(security_audit, "append_event", unavailable)
    with pytest.raises(RuntimeError, match="audit failure"):
        await service.set_backend(agent, "anthropic", 0, actor)
    assert await service.current(agent) is None


async def test_agent_effective_revisions_do_not_become_channel_overrides(owned) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, "openai", 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="chan")
    async with sessions.begin() as session:
        first, shared, source = await resolve_turn_backend(
            session, channel, agent.name, enabled=True
        )
        assert first is not None and source == "agent" and not shared
    async with sessions.begin() as session:
        assert await current_backend(session, channel) is None
        repeated, _, _ = await resolve_turn_backend(session, channel, agent.name, enabled=True)
        assert repeated == first
        assert await mux_state.put_config_revision(session, first) == first
        assert await current_backend(session, channel) is None
    await service.set_backend(agent, "gemini", 1, actor)
    async with sessions.begin() as session:
        second, _, _ = await resolve_turn_backend(session, channel, agent.name, enabled=True)
        assert second is not None and second.local > first.local
        assert (
            await resolve_turn_backend(
                session, channel, agent.name, enabled=True, retained_revision=first.local
            )
        )[0] == first
        explicit = await set_channel_backend(session, channel, BackendConfig())
        assert explicit.local > second.local
        assert (await resolve_turn_backend(session, channel, agent.name, enabled=True))[
            0
        ] == explicit


async def test_disallowed_retained_backend_is_refused_by_same_admission_gate(owned) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, "openai", 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="chan")
    async with sessions.begin() as session:
        revision, _, _ = await resolve_turn_backend(session, channel, agent.name, enabled=True)
    assert revision is not None
    with pytest.raises(BackendUnsupported):
        check_backend(revision, enabled_backends=("anthropic",))


def test_factory_uses_existing_flags() -> None:
    assert not make_backend_service(None, TurnSettings()).enabled
    assert not make_backend_service(None, TurnSettings(path="mux")).enabled
    assert make_backend_service(None, TurnSettings(path="mux", channel_backends=True)).enabled


async def test_public_loader_uses_actual_scoped_sdk_projection_and_never_updates(owned) -> None:
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.testing.ma import list_response
    from daimon.testing.ma_models import ma_agent
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    _, agent, _, sessions = owned
    native = ma_agent(
        id="agent-owned", tenant_id=str(agent.tenant_id), name=agent.name, model="claude-opus-5-5"
    )
    payload = native.model_dump(mode="json")
    payload["metadata"]["daimon_name"] = agent.name
    trace = ScriptedTransport()
    trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload])))
    async with trace.client() as client:
        loaded = await load_backend_agent(
            client, sessions, tenant_id=agent.tenant_id, agent_name=agent.name
        )
    assert loaded.agent_id == derive_agent_uuid(
        tenant_id=agent.tenant_id, ma_agent_id="agent-owned"
    )
    assert loaded.model == "claude-opus-5-5" and loaded.configuration_digest
    assert [(r.method, r.path) for r in trace.requests] == [("GET", "/v1/agents")]
    assert not trace.violations and not trace.replies


async def test_audit_filters_before_pagination_and_unknown_accounts_are_not_accepted(owned) -> None:
    service, agent, actor, sessions = owned
    neighbor = agent.model_copy(update={"agent_id": uuid.uuid4(), "name": "neighbor"})
    await service.set_backend(neighbor, "anthropic", 0, actor)
    first = await service.set_backend(agent, "anthropic", 0, actor)
    rows = await service.audit(agent.tenant_id, agent_id=agent.agent_id, limit=1)
    assert len(rows) == 1 and rows[0].scope == f"agent_backend:{agent.agent_id}:{first.local}"
    assert await service.audit(uuid.uuid4(), agent_id=agent.agent_id) == ()
    with pytest.raises(AgentBackendRefused, match="actor not found"):
        await service.set_backend(agent, "anthropic", 1, replace(actor, account_id=uuid.uuid4()))


async def test_factory_preserves_actual_agent_model_and_unknown_native_rates(owned) -> None:
    _, agent, _, sessions = owned
    settings = TurnSettings(path="mux", channel_backends=True)
    service = make_backend_service(sessions, settings)
    opus = await service.preview(agent.model_copy(update={"model": "claude-opus-5-5"}), "anthropic")
    assert "claude-opus-5-5" in opus.cost_line
    assert "haiku" not in opus.cost_line.lower()
    native = await service.preview(agent, "openai")
    assert "unknown" in native.cost_line.lower() and "worst" in native.cost_line.lower()


async def test_default_revision_reput_does_not_become_override_or_hide_agent_setting(owned) -> None:
    from mux.contracts.config import ConfigRevision, resolve_default

    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, "openai", 0, actor)
    channel = ChannelRef(
        tenant_id=str(agent.tenant_id), platform="discord", channel_id="default-pin"
    )
    default = ConfigRevision.create(channel, 0, resolve_default(None))
    async with sessions.begin() as session:
        await mux_state.put_config_revision(session, default)
        assert await current_backend(session, channel) is None
        assert await mux_state.latest_config_revision(session, channel) == default
        effective, _, source = await resolve_turn_backend(
            session, channel, agent.name, enabled=True
        )
        assert effective is not None and source == "agent" and effective.backend == "openai"
    async with sessions() as session:
        assert await resolve_turn_backend(
            session, channel, agent.name, enabled=True, retained_revision=0
        ) == (None, False, "default")
