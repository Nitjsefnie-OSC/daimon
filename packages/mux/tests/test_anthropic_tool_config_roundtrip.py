"""Retrieved builtin config fields survive the authorized session override boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from anthropic.types.beta.session_create_params import Agent
from daimon.core.session_snapshot import session_tools
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.testing.ma_models import ma_agent, ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import SessionSpec
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from pydantic import JsonValue, ValidationError

FIXTURES = Path(__file__).parent / "fixtures"
SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)


def reference(kind: str, identity: str) -> ResourceRef:
    return ResourceRef(
        id=identity,
        kind=kind,
        provider="anthropic",
        account_scope_id="org",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )


def session_spec(agent: dict[str, JsonValue]) -> SessionSpec:
    extension = ExtensionConfig(
        namespace="anthropic.session_create", version=1, value={"agent": agent}
    )
    return SessionSpec(
        agent=reference("agent", "ag_1"),
        environment=reference("environment", "env_1"),
        agent_revision=Revision(local=0),
        config_revision=0,
        extensions={extension.namespace: extension},
    )


@pytest.mark.parametrize(
    "fixture", ["anthropic_legacy_builtin_tools.json", "anthropic_retrieved_builtin_tools.json"]
)
async def test_sdk_retrieval_and_session_override_preserve_exact_request_bytes(
    fixture: str,
) -> None:
    tools = json.loads((FIXTURES / fixture).read_text())
    retrieved = ma_agent(id="ag_1", tools=tools).model_dump(mode="json")
    main, port_transport = ScriptedTransport(), ScriptedTransport()
    for transport in (main, port_transport):
        transport.queue(
            ScriptedReply("GET", "/v1/agents/ag_1", httpx.Response(200, json=retrieved))
        )
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions",
                httpx.Response(200, json=ma_session().model_dump(mode="json")),
            )
        )
    async with main.client() as original, port_transport.client() as client:
        for sdk, through_port in ((original, False), (client, True)):
            agent = await sdk.beta.agents.retrieve("ag_1")
            effective = session_tools(
                agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=None
            )
            override: dict[str, JsonValue] = {
                "type": "agent_with_overrides",
                "id": agent.id,
                "model": "claude-haiku-5-5",
                "mcp_servers": [],
                "tools": [
                    cast(JsonValue, tool.model_dump(mode="json", exclude_none=True))
                    for tool in effective
                ],
            }
            if through_port:
                port = AnthropicSessions(
                    sdk,
                    "org",
                    ResourceAuthorization(
                        SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})
                    ),
                )
                await port.create(SCOPE, session_spec(override), key="create")
            else:
                await sdk.beta.sessions.create(agent=cast(Agent, override), environment_id="env_1")
    main.assert_consumed()
    port_transport.assert_consumed()
    assert main.requests == port_transport.requests
    body = main.requests[1].json()
    assert isinstance(body, dict)
    override_body = body["agent"]
    assert isinstance(override_body, dict)
    assert override_body["tools"] == tools


@pytest.mark.parametrize(
    "change",
    [
        {"type": True},
        {"type": None},
        {"type": 123},
        {"type": []},
        {"type": {}},
        {"type": "unapproved"},
        {"type": "read"},
        {"unexpected": "field"},
        {"enabled": "true"},
        {"name": "unapproved"},
        {"permission_policy": {"type": "unapproved"}},
    ],
)
async def test_builtin_config_acceptance_keeps_the_native_fence(
    change: dict[str, JsonValue],
) -> None:
    config: dict[str, JsonValue] = {
        "name": "bash",
        "enabled": True,
        "permission_policy": {"type": "always_allow"},
        **change,
    }
    agent: dict[str, JsonValue] = {
        "type": "agent_with_overrides",
        "id": "ag_1",
        "tools": [{"type": "agent_toolset_20260401", "configs": [config]}],
    }
    transport = ScriptedTransport()
    async with transport.client() as sdk:
        port = AnthropicSessions(
            sdk,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
        )
        with pytest.raises(ValidationError):
            await port.create(SCOPE, session_spec(agent), key="create")
    assert not transport.requests
