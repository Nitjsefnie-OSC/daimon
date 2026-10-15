"""Native model settings survive source projection and ordinary session creation."""

import json
import uuid
from pathlib import Path

import httpx
import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core.agent_fork import copy_agent
from daimon.core.authz import Subject
from daimon.core.channel_backend import load_backend_agent
from daimon.core.mux_compat import agent_spec
from daimon.core.sessions import create_session, project_session_resources
from daimon.core.turn.runtime_manifest import preview_source_drops
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import ModelRef, Scope
from mux.drivers.anthropic.resources.agents import agent_payload


@pytest.fixture
def recorded_root():
    return json.loads((Path(__file__).parent / "fixtures/anthropic_live_root.json").read_text())


def native_root(recorded_root, model, tenant):
    agent = ma_agent(name="daimon-haiku55", tenant_id=tenant, version=10)
    return BetaManagedAgentsAgent.model_validate(
        {
            **agent.model_dump(mode="json"),
            **{k: v for k, v in recorded_root.items() if k != "environment"},
            "model": model,
        }
    )


@pytest.mark.parametrize("effort", ["low", {"type": "medium"}, "high", "xhigh", "max"])
async def test_real_root_model_survives_session_source_projection(
    db_session_factory, recorded_root, effort
):
    tenant, account = uuid.uuid4(), uuid.uuid4()
    model = {"id": "claude-haiku-5-5", "speed": "standard", "effort": effort}
    agent = native_root(recorded_root, model, tenant)
    environment = BetaEnvironment.model_validate(
        {**ma_environment(tenant_id=tenant).model_dump(mode="json"), **recorded_root["environment"]}
    )
    scope = Scope(
        tenant_id=str(tenant),
        account_id=str(account),
        principal_id="caller",
        authorization_id="admitted",
    )
    async with ScriptedTransport().client() as client:
        source = await load_backend_agent(
            client, db_session_factory, tenant_id=tenant, agent_name=agent.name, actual_agent=agent
        )
    projected = await project_session_resources(
        source,
        agent=agent,
        environment=environment,
        session_factory=db_session_factory,
        tenant_id=tenant,
        agent_uuid=source.agent_id,
        account_id=account,
        resource_scope=scope,
        memory_read_only=False,
    )
    target = preview_source_drops(projected, ModelRef(provider="anthropic", id=model["id"]))
    assert target.declared_drops == ()
    payload = agent_payload(target)
    assert payload["model"] == model
    assert payload["tools"] == recorded_root["tools"]
    assert payload["skills"] == recorded_root["skills"]
    assert payload["mcp_servers"] == recorded_root["mcp_servers"]


async def test_ordinary_native_session_references_root_without_recompiling_its_model(recorded_root):
    model = {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    agent = native_root(recorded_root, model, uuid.uuid4())
    # The compiler boundary is exercised separately; an ordinary session pins the existing agent.
    assert agent_payload(agent_spec({"name": agent.name, "model": model}))["model"] == model
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST", "/v1/sessions", httpx.Response(200, json=ma_session().model_dump(mode="json"))
        )
    )
    async with transport.client() as client:
        await create_session(client, agent=agent, environment=ma_environment())
    transport.assert_consumed()
    body = transport.requests[0].json()
    assert body["agent"] == agent.id
    assert "model" not in body
    assert agent.model.model_dump(mode="json", exclude_unset=True) == model


async def test_ordinary_agent_copy_preserves_resolved_effort_without_backend_switch_flags(
    db_session, db_session_factory, monkeypatch
):
    tenant = await make_tenant(db_session)
    model = {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    source = BetaManagedAgentsAgent.model_validate(
        {**ma_agent(id="source", tenant_id=tenant.id).model_dump(mode="json"), "model": model}
    )
    copied = source.model_copy(update={"id": "copied", "name": "copy"})
    monkeypatch.setattr("daimon.core.agent_fork.queue_agent_face", lambda *a, **kw: None)
    transport = ScriptedTransport()
    for method, path, agent in (
        ("GET", "/v1/agents/source", source),
        ("POST", "/v1/agents", copied),
    ):
        transport.queue(
            ScriptedReply(method, path, httpx.Response(200, json=agent.model_dump(mode="json")))
        )
    async with transport.client() as client:
        await copy_agent(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            source=source,
            new_name="copy",
            public_url=None,
            subject=Subject(is_admin=True),
            default_agent_name="daimon",
        )
    transport.assert_consumed()
    assert transport.requests[1].json()["model"] == model
