"""Watch posture closes shell/built-in and deployment-write allowance escapes."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.continuity.watch_turn import constrain_session, read_report_policy, watch_posture
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.testing import ma_agent, ma_session
from mux.contracts.ids import Scope


@pytest.mark.parametrize(
    "server,tool,allowed",
    [
        ("linear", "get_issue", True),
        ("linear", "create_issue", False),
        (None, "bash", False),
        (None, "execute_code", False),
        ("sandbox", "bash", False),
        ("daimon-mcp", "create_agent", False),
        ("daimon-mcp", "call_tool", False),
        ("linear", "get_or_create", False),
    ],
)
async def test_watch_read_report_refuses_writes_shell_and_builtins_even_with_write_grants(
    server, tool, allowed
):
    policy = ToolSafetyPolicy(
        enabled=False,
        unattended_writes=("*",),
        agent_rules=True,
        effects={"linear/create_issue": "read"},
    )
    result = await watch_posture(policy).decide(
        ToolCall(tool_use_id="blocked", server_name=server, tool_name=tool)
    )
    assert result.allow is allowed
    strict = read_report_policy(policy)
    assert strict.enabled and not strict.unattended_writes
    assert strict.agent_rules == policy.agent_rules


async def test_watch_gate_removes_shell_skills_and_builtin_server_before_send(monkeypatch):
    from daimon.core.continuity import watch_turn as module

    snapshot = ma_session(
        agent=ma_agent(
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "daimon-mcp",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "linear",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
            ],
            mcp_servers=[
                {"name": "daimon-mcp", "type": "url", "url": "https://daimon.invalid/mcp"},
                {"name": "linear", "type": "url", "url": "https://linear.invalid/mcp"},
            ],
        )
    )
    update = AsyncMock()
    gate = AsyncMock()
    monkeypatch.setattr(module, "update_session_record", update)
    monkeypatch.setattr(module, "gate_session_snapshot", gate)
    await constrain_session(
        MagicMock(),
        snapshot,
        ToolSafetyPolicy(),
        scope=Scope(
            tenant_id=str(uuid.uuid4()),
            account_id=str(uuid.uuid4()),
            principal_id="watch-owner",
            authorization_id="test-grant",
        ),
        public_url=None,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    agent = update.await_args.kwargs["agent"]
    assert "skills" not in agent
    assert [t["mcp_server_name"] for t in agent["tools"]] == ["linear"]
    assert [s["name"] for s in agent["mcp_servers"]] == ["linear"]
    gate.assert_awaited_once()
    assert gate.await_args.args[2].enabled


async def test_watch_turn_requires_explicit_unattended_before_any_work():
    from datetime import UTC, datetime

    from daimon.core.stores.domain import TaskContinuationRow
    from daimon.core.turn.run import run_prepared_turn_impl

    row = TaskContinuationRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="slack",
        thread_id="t",
        parent_channel_id="c",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="u",
        target_ma_agent_id="a",
        target_name="a",
        requested_work="report",
        reason="watch_fired",
        status="pending",
        skip_reason=None,
        idempotency_key=uuid.uuid4(),
        created_at=datetime.now(UTC),
        claimed_at=None,
        delivered_at=None,
    )
    with pytest.raises(ValueError, match="explicitly unattended"):
        await run_prepared_turn_impl(
            MagicMock(),
            MagicMock(),
            tenant_id=row.tenant_id,
            platform="slack",
            thread_id="t",
            external_user_id="u",
            user_message="report",
            lifecycle=MagicMock(),
            cancel=MagicMock(),
            watch_row=row,
            attended=True,
            reseed_user_message=AsyncMock(),
            recovery_lifecycle=MagicMock(),
        )


async def test_watch_reads_recheck_current_agent_denials_without_allowing_writes():
    from daimon.core.tool_safety import ResolvedAgentToolRule

    current = "allow"

    async def lookup(call):
        return ResolvedAgentToolRule(rule=current, scope="chat_and_routines")

    posture = watch_posture(ToolSafetyPolicy(agent_rules=True), rules_for=lookup)
    read = ToolCall(tool_use_id="r", server_name="linear", tool_name="get_issue")
    assert (await posture.decide(read)).allow
    current = "deny"
    assert not (await posture.decide(read)).allow
    current = "allow"
    assert not (
        await posture.decide(
            ToolCall(tool_use_id="w", server_name="linear", tool_name="create_issue")
        )
    ).allow
