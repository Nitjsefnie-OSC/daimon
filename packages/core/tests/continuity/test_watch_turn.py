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


@pytest.mark.parametrize("session_url", [False, True])
async def test_watch_gate_removes_shell_skills_and_builtin_server_before_send(
    monkeypatch, session_url
):
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
                    "configs": [
                        {
                            "name": "get_issue",
                            "enabled": True,
                            "permission_policy": {"type": "always_allow"},
                        }
                    ],
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
    from daimon.core.mcp_server_url import session_mcp_url

    public_url = "https://DAIMON.invalid:443/mcp/"
    own_url = "https://daimon.invalid/mcp"
    if session_url:
        own_url = session_mcp_url(own_url, str(uuid.uuid4()))
    snapshot.agent.tools.append(
        snapshot.agent.tools[1].model_copy(update={"mcp_server_name": "renamed-management"})
    )
    snapshot.agent.mcp_servers.append(
        snapshot.agent.mcp_servers[0].model_copy(
            update={"name": "renamed-management", "url": own_url}
        )
    )
    update = AsyncMock()
    monkeypatch.setattr(module, "update_session_record", update)
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
        public_url=public_url,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    agent = update.await_args.kwargs["agent"]
    assert "skills" not in agent
    assert [t["mcp_server_name"] for t in agent["tools"]] == ["linear"]
    assert [s["name"] for s in agent["mcp_servers"]] == ["linear"]
    update.assert_awaited_once()
    assert agent["tools"][0]["default_config"]["enabled"] is False
    assert agent["tools"][0]["configs"][0]["permission_policy"] == {"type": "always_ask"}


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


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_restore_finishes_before_cancellation_releases_watch_session(
    monkeypatch, cancellations
):
    import asyncio

    from daimon.core.continuity import watch_turn

    started = asyncio.Event()
    finish = asyncio.Event()
    restored = asyncio.Event()
    snapshot = ma_session()

    async def update(*args, **kwargs):
        started.set()
        await finish.wait()
        assert kwargs["agent"]["tools"] == [
            tool.model_dump(mode="json") for tool in snapshot.agent.tools
        ]
        restored.set()

    monkeypatch.setattr(watch_turn, "update_session_record", update)
    task = asyncio.create_task(
        watch_turn.restore_session_tools(
            MagicMock(),
            snapshot,
            scope=Scope(
                tenant_id=str(uuid.uuid4()),
                account_id=str(uuid.uuid4()),
                principal_id="watch-owner",
                authorization_id="test-grant",
            ),
        )
    )
    await started.wait()
    for _ in range(cancellations):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert restored.is_set()


async def test_watch_refuses_skill_session_before_any_provider_mutation(monkeypatch):
    from daimon.core.continuity import watch_turn
    from daimon.core.turn.errors import AdmissionDenied

    update = AsyncMock()
    monkeypatch.setattr(watch_turn, "update_session_record", update)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await constrain_session(
            MagicMock(),
            ma_session(
                agent=ma_agent(
                    skills=[{"type": "custom", "skill_id": "skill_test", "version": "latest"}]
                )
            ),
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
    update.assert_not_awaited()


async def test_watch_refuses_multiagent_session_whose_subagent_has_shell(monkeypatch):
    from anthropic.types.beta.beta_managed_agents_session_multiagent_coordinator import (
        BetaManagedAgentsSessionMultiagentCoordinator,
    )
    from daimon.core.continuity import watch_turn

    SHELL = {
        "type": "agent_toolset_20260401",
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }
    from daimon.core.turn.errors import AdmissionDenied

    update = AsyncMock()
    monkeypatch.setattr(watch_turn, "update_session_record", update)
    base = ma_session(agent=ma_agent())
    sub = ma_session(agent=ma_agent(id="agent_sub", name="sub", tools=[SHELL])).agent
    coordinator = BetaManagedAgentsSessionMultiagentCoordinator.model_validate(
        {"type": "coordinator", "agents": [sub.model_dump(mode="json")]}
    )
    snapshot = base.model_copy(
        update={"agent": base.agent.model_copy(update={"multiagent": coordinator})}
    )
    with pytest.raises(AdmissionDenied):
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
    # If we got here without raising, the update below proves the coordinator stayed.
    update.assert_not_awaited()


def _explicit_read_snapshot():
    return ma_session(
        agent=ma_agent(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "linear",
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                    "configs": [
                        {
                            "name": name,
                            "enabled": enabled,
                            "permission_policy": {"type": "always_allow"},
                        }
                        for name, enabled in [
                            ("get_issue", True),
                            ("create_issue", True),
                            ("get_secret", False),
                            ("future_action", True),
                        ]
                    ],
                }
            ],
            mcp_servers=[{"name": "linear", "type": "url", "url": "https://linear.invalid/mcp"}],
        )
    ).model_copy(deep=True)


@pytest.mark.parametrize(
    "level",
    [
        "session",
        "agent",
        "model",
        "server",
        "tool",
        "default",
        "config",
        "policy",
        "stats",
        "usage",
    ],
)
@pytest.mark.parametrize("value", [None, False, [], {"enabled": True}])
async def test_watch_refuses_unknown_capability_fields_before_mutation(monkeypatch, level, value):
    from daimon.core.continuity import watch_turn
    from daimon.core.turn.errors import AdmissionDenied

    snapshot = _explicit_read_snapshot()
    targets = {
        "stats": snapshot.stats,
        "usage": snapshot.usage,
        "session": snapshot,
        "agent": snapshot.agent,
        "model": snapshot.agent.model,
        "server": snapshot.agent.mcp_servers[0],
        "tool": snapshot.agent.tools[0],
        "default": snapshot.agent.tools[0].default_config,
        "config": snapshot.agent.tools[0].configs[0],
        "policy": snapshot.agent.tools[0].configs[0].permission_policy,
    }
    # Simulate a new response field, including an empty value: unknown is never safe.
    targets[level].__pydantic_extra__["future_execution_capability"] = value
    update = AsyncMock()
    monkeypatch.setattr(watch_turn, "update_session_record", update)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await constrain_session(
            MagicMock(),
            snapshot,
            ToolSafetyPolicy(),
            scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
            public_url=None,
            sessions=MagicMock(),
            tenant_id=uuid.uuid4(),
        )
    update.assert_not_awaited()


def test_watch_refuses_capability_newly_recognized_by_sdk():
    from daimon.core.continuity.watch_turn import validate_watch_snapshot
    from daimon.core.turn.errors import AdmissionDenied
    from pydantic import create_model

    snapshot = ma_session()
    agent_type = create_model(
        "FutureAgent", __base__=type(snapshot.agent), future_execution_capability=(bool, True)
    )
    snapshot = snapshot.model_copy(
        update={"agent": agent_type.model_validate(snapshot.agent.model_dump())}
    )
    with pytest.raises(AdmissionDenied):
        validate_watch_snapshot(snapshot)


async def test_watch_installs_only_explicit_read_configs_with_disabled_default(monkeypatch):
    from daimon.core.continuity import watch_turn

    update = AsyncMock()
    monkeypatch.setattr(watch_turn, "update_session_record", update)
    await constrain_session(
        MagicMock(),
        _explicit_read_snapshot(),
        ToolSafetyPolicy(enabled=False, unattended_writes=("*",)),
        scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
        public_url=None,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    update.assert_awaited_once()
    agent = update.await_args.kwargs["agent"]
    assert agent["tools"] == [
        {
            "type": "mcp_toolset",
            "mcp_server_name": "linear",
            "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            "configs": [
                {"name": "get_issue", "enabled": True, "permission_policy": {"type": "always_ask"}}
            ],
        }
    ]


async def test_watch_does_not_expand_default_only_toolset(monkeypatch):
    from daimon.core.continuity import watch_turn

    snapshot = _explicit_read_snapshot()
    snapshot.agent.tools[0].configs.clear()
    update = AsyncMock()
    monkeypatch.setattr(watch_turn, "update_session_record", update)
    await constrain_session(
        MagicMock(),
        snapshot,
        ToolSafetyPolicy(),
        scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
        public_url=None,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    assert update.await_args.kwargs["agent"] == {"tools": [], "mcp_servers": []}


@pytest.mark.parametrize("name", ["get_*", "get_[issue]", "get_?", "get_issue;delete", "*"])
async def test_watch_tool_allowlist_never_contains_patterns(monkeypatch, name):
    from daimon.core.continuity import watch_turn

    snapshot = _explicit_read_snapshot()
    snapshot.agent.tools[0].configs = [
        snapshot.agent.tools[0].configs[0].model_copy(update={"name": name})
    ]
    update = AsyncMock()
    monkeypatch.setattr(watch_turn, "update_session_record", update)
    await constrain_session(
        MagicMock(),
        snapshot,
        ToolSafetyPolicy(),
        scope=Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="g"),
        public_url=None,
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    assert update.await_args.kwargs["agent"] == {"tools": [], "mcp_servers": []}


def test_watch_accepts_sdk_request_id_as_diagnostic_metadata():
    from anthropic._models import add_request_id
    from daimon.core.continuity.watch_turn import validate_watch_snapshot

    snapshot = _explicit_read_snapshot()
    add_request_id(snapshot, "req_watch_validation")
    validate_watch_snapshot(snapshot)
