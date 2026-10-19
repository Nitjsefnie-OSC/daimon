"""The one unattended gate: its snapshot rewrite and its decider."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.core.mux_backend import platform_scope
from daimon.core.tool_safety import (
    ToolAnnotations,
    ToolCall,
    ToolSafetyPolicy,
    classify_tool,
)
from daimon.core.turn.posture import PolicyApproval
from daimon.core.turn.unattended import gate_session_snapshot, unattended_posture
from daimon.testing import ma_agent, ma_session
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body

_LINEAR = {
    "type": "mcp_toolset",
    "mcp_server_name": "linear",
    "configs": [
        {"name": "create_issue", "enabled": True, "permission_policy": {"type": "always_allow"}}
    ],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}


def _session() -> Any:
    return ma_session(
        id="ses_gate",
        agent=ma_agent(
            tools=[_LINEAR],
            mcp_servers=[{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}],
        ),
    )


def _call(tool: str) -> ToolCall:
    return ToolCall(tool_use_id="tu_1", server_name="linear", tool_name=tool)


@pytest.mark.parametrize(
    ("policy", "tool", "allowed"),
    [
        (ToolSafetyPolicy(enabled=False), "create_issue", True),
        (ToolSafetyPolicy(enabled=True), "create_issue", False),
        (ToolSafetyPolicy(enabled=True), "get_issue", True),
        (ToolSafetyPolicy(enabled=True, unattended_writes=("linear",)), "create_issue", True),
    ],
)
async def test_posture_is_always_the_unattended_decider(
    policy: ToolSafetyPolicy, tool: str, allowed: bool
) -> None:
    """Never `AutoApprove`: off is the explicit policy, and the decider reads it."""
    posture = unattended_posture(
        policy,
        session=_session(),
        public_url=None,
        session_factory=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    assert isinstance(posture, PolicyApproval)
    assert (await posture.decide(_call(tool))).allow is allowed


async def test_posture_reads_annotations_only_through_its_seam() -> None:
    seen: list[str] = []

    async def lookup(call: ToolCall) -> ToolAnnotations | None:
        seen.append(call.key)
        return ToolAnnotations(read_only_hint=True)

    posture = unattended_posture(
        ToolSafetyPolicy(enabled=True),
        session=_session(),
        public_url=None,
        session_factory=MagicMock(),
        tenant_id=uuid.uuid4(),
        annotations_for=lookup,
    )
    assert (await posture.decide(_call("memory_flag_list"))).allow
    assert seen == ["linear/memory_flag_list"]


def test_a_destructive_hint_wins_over_read_only() -> None:
    policy = ToolSafetyPolicy(enabled=True)
    both = ToolAnnotations(read_only_hint=True, destructive_hint=True)
    assert classify_tool(policy, server_name="s", tool_name="get_x", annotations=both) == "write"


@pytest.mark.parametrize("enabled", [False, True])
async def test_gate_rewrites_default_and_per_tool_configs_or_nothing(enabled: bool) -> None:
    updates: list[dict[str, Any]] = []

    def update(request: httpx.Request, _m: Any) -> httpx.Response:
        updates.append(json_body(request)["agent"])
        return httpx.Response(200, json=_session().model_dump(mode="json"))

    router = MARouter()
    router.add("POST", r"/v1/sessions/ses_gate$", update)
    client = build_fake_anthropic(router.dispatch)
    try:
        await gate_session_snapshot(
            client,
            _session(),
            ToolSafetyPolicy(enabled=enabled),
            scope=platform_scope("test unattended gate"),
            public_url=None,
            session_factory=MagicMock(),
            tenant_id=uuid.uuid4(),
        )
    finally:
        await client.close()
    if not enabled:
        assert updates == []
        return
    [agent] = updates
    toolset = agent["tools"][0]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    assert [c["permission_policy"] for c in toolset["configs"]] == [{"type": "always_ask"}]
    assert agent["mcp_servers"][0]["url"] == "https://linear.example/mcp"
