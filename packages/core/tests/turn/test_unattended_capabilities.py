"""Known unattended capability gaps, exercised through a fake MA transport."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.core.mux_backend import platform_scope
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.core.turn.driver import run_turn, unattended_sends
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.unattended import session_gate, unattended_posture
from daimon.testing import ma_agent, ma_session
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, send_events_response
from daimon.testing.ma_sessions import session_turn_sse
from daimon.testing.turn_fakes import RecordingLifecycle

from .conftest import make_status_idle


def _coordinator_session() -> BetaManagedAgentsSession:
    allow = {"enabled": True, "permission_policy": {"type": "always_allow"}}
    tools = [
        {
            "type": "agent_toolset_20260401",
            "default_config": allow,
            "configs": [{"name": name, **allow} for name in ("bash", "write", "edit")],
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": "linear",
            "default_config": allow,
            "configs": [{"name": "create_issue", **allow}],
        },
    ]
    servers = [{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}]
    session = ma_session(
        id="ses_coordinator",
        agent=ma_agent(id="agent_coordinator", tools=tools, mcp_servers=servers),
    ).model_dump(mode="json")
    child = ma_session(
        agent=ma_agent(id="agent_worker", tools=tools, mcp_servers=servers)
    ).agent.model_dump(mode="json", exclude={"multiagent"})
    session["agent"]["multiagent"] = {"type": "coordinator", "agents": [child]}
    # Validate the full provider shape: the roster contains definitions, not IDs.
    return BetaManagedAgentsSession.model_validate(session)


def _can_run_without_decider(toolset: dict[str, Any], name: str) -> bool:
    config = next((c for c in toolset["configs"] if c["name"] == name), None)
    effective: dict[str, Any] = {**toolset["default_config"], **(config or {})}
    return effective["enabled"] and effective["permission_policy"]["type"] != "always_ask"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="S1 gates only root MCP arrays; coordinator and built-in capabilities survive",
)
@pytest.mark.parametrize(
    "capability", ["coordinator", "root_builtin", "child_builtin", "child_mcp"]
)
async def test_unattended_send_has_no_ungated_write_capability(capability: str) -> None:
    """A real driver send observes the provider's post-gate effective snapshot.

    The fake applies only supplied update fields, matching MA's partial agent
    update contract. It does not simulate tool execution or call live services.
    """
    snapshot = _coordinator_session()
    provider = snapshot.model_dump(mode="json")
    updates: list[dict[str, Any]] = []
    at_send: list[dict[str, Any]] = []

    def update(request: httpx.Request, _match: Any) -> httpx.Response:
        agent = json_body(request)["agent"]
        updates.append(agent)
        provider["agent"].update(agent)
        return httpx.Response(200, json=provider)

    def send(request: httpx.Request, _match: Any) -> httpx.Response:
        assert json_body(request)["events"][0]["type"] == "user.message"
        at_send.append(deepcopy(provider["agent"]))
        return send_events_response([])

    router = MARouter()
    router.add(
        "GET", r"/v1/sessions/ses_coordinator$", lambda _r, _m: httpx.Response(200, json=provider)
    )
    router.add("POST", r"/v1/sessions/ses_coordinator$", update)
    router.add("POST", r"/v1/sessions/ses_coordinator/events$", send)
    router.add(
        "GET",
        r"/v1/sessions/ses_coordinator/events/stream$",
        lambda _r, _m: session_turn_sse(make_status_idle(event_id="sevt_done")),
    )
    client = build_fake_anthropic(router.dispatch)
    policy = ToolSafetyPolicy(enabled=True)
    try:
        with unattended_sends(
            session_gate(
                client,
                snapshot.id,
                policy,
                scope=platform_scope("unattended coordinator regression"),
                public_url=None,
                session_factory=None,
                tenant_id=None,
            )
        ):
            result = await run_turn(
                anthropic=client,
                session_id=snapshot.id,
                user_message="nightly work",
                lifecycle=RecordingLifecycle(),
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="cli-operator-run"),
                path="legacy",
                tool_confirmation=unattended_posture(
                    policy,
                    session=snapshot,
                    public_url=None,
                    session_factory=None,
                    tenant_id=None,
                ),
            )
    finally:
        await client.close()

    assert result.error is None
    [agent] = at_send
    assert updates, "the real unattended gate must run before the message"
    root_mcp = next(t for t in agent["tools"] if t["type"] == "mcp_toolset")
    assert not _can_run_without_decider(root_mcp, "create_issue"), "root MCP gating works"

    # Each parameter fails on a distinct surviving capability, after the send.
    if capability == "coordinator":
        assert agent.get("multiagent") is None, (
            "unattended send retains a spawnable coordinator roster"
        )
    elif capability == "root_builtin":
        builtins = [t for t in agent["tools"] if t["type"] == "agent_toolset_20260401"]
        assert not any(
            _can_run_without_decider(t, name)
            for t in builtins
            for name in ("bash", "write", "edit")
        ), "root built-in shell/write/edit still bypass the decider"
    elif agent.get("multiagent") is not None:
        child = agent["multiagent"]["agents"][0]
        kind = "mcp_toolset" if capability == "child_mcp" else "agent_toolset_20260401"
        names = ("create_issue",) if capability == "child_mcp" else ("bash", "write", "edit")
        assert not any(
            _can_run_without_decider(t, name)
            for t in child["tools"]
            if t["type"] == kind
            for name in names
        ), f"{capability} in the coordinator roster still bypasses the decider"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="S1's unattended decider treats built-in calls as not_attached and allows them",
)
@pytest.mark.parametrize("name", ["bash", "write", "edit"])
async def test_unattended_decider_refuses_builtin_writes(name: str) -> None:
    """Changing only built-in permission policies to always_ask is insufficient."""
    posture = unattended_posture(
        ToolSafetyPolicy(enabled=True),
        session=_coordinator_session(),
        public_url=None,
        session_factory=None,
        tenant_id=None,
    )
    decision = await posture.decide(
        ToolCall(tool_use_id="tu_builtin", server_name=None, tool_name=name)
    )
    assert not decision.allow, f"unattended decider still allows the built-in {name} tool"
