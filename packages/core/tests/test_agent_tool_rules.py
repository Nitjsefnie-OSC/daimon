"""Agent tool rule precedence, live reads and revision history."""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from daimon.core._models import AgentToolRuleHistoryRecord, AgentToolRuleRecord, Tenant
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.confirmation import (
    ConfirmationAnswer,
    ConfirmationPrompt,
    ToolRuleCardReference,
)
from daimon.core.stores import agent_tool_rules as store
from daimon.core.tool_safety import (
    ResolvedAgentToolRule,
    ToolCall,
    ToolSafetyPolicy,
    decide_tool_call,
    stricter_agent_tool_rule,
)
from daimon.core.turn.approvals import (
    channel_for_tool_rules,
    interactive_decider,
    unattended_decider,
)
from sqlalchemy import Index, Table, select
from sqlalchemy.ext.asyncio import AsyncSession


def _call(name: str = "append_note", server: str = "remote") -> ToolCall:
    return ToolCall(tool_use_id="one", server_name=server, tool_name=name)


@pytest.mark.parametrize(
    ("rule", "attended", "scope", "expected"),
    [
        ("allow", True, "chat", "allow"),
        ("allow", False, "chat", "allow"),
        ("allow", False, "chat_and_routines", "allow"),
        ("ask", True, "chat", "ask"),
        ("ask", False, "chat_and_routines", "deny"),
        ("deny", True, "chat", "deny"),
        ("deny", False, "chat", "deny"),
    ],
)
def test_rule_decision(rule: str, attended: bool, scope: str, expected: str) -> None:
    policy = ToolSafetyPolicy(enabled=True, agent_rules=True, unattended_writes=("*",))
    resolved = ResolvedAgentToolRule.model_validate({"rule": rule, "scope": scope})
    assert (
        decide_tool_call(policy, _call(), attended=attended, agent_rule=resolved).outcome
        == expected
    )


def test_rule_precedence_and_own_tools() -> None:
    allow = ResolvedAgentToolRule(rule="allow")
    denied = ToolSafetyPolicy(enabled=True, agent_rules=True, denied=("remote/append_note",))
    assert (
        decide_tool_call(denied, _call(), attended=True, agent_rule=allow).reason
        == "denied_by_operator"
    )
    ask = ResolvedAgentToolRule(rule="ask")
    policy = ToolSafetyPolicy(enabled=True, agent_rules=True)
    assert (
        decide_tool_call(policy, _call("list_items"), attended=True, agent_rule=ask).outcome
        == "ask"
    )
    assert (
        decide_tool_call(
            policy,
            _call("add_skill", "daimon-mcp"),
            attended=True,
            agent_rule=ask,
            trusted_servers=frozenset({"daimon-mcp"}),
        ).reason
        != "agent_rule_ask"
    )
    off = ToolSafetyPolicy(enabled=True)
    assert decide_tool_call(off, _call(), attended=True, agent_rule=allow) == decide_tool_call(
        off, _call(), attended=True
    )
    chat_only = ResolvedAgentToolRule(rule="allow", scope="chat")
    assert decide_tool_call(policy, _call(), attended=False, agent_rule=chat_only).outcome == "deny"
    pinned_write = ToolSafetyPolicy(
        enabled=True, agent_rules=True, effects={"remote/append_note": "write"}
    )
    assert (
        decide_tool_call(pinned_write, _call(), attended=True, agent_rule=allow).outcome == "allow"
    )
    pinned_read = ToolSafetyPolicy(
        enabled=True, agent_rules=True, effects={"remote/append_note": "read"}
    )
    assert decide_tool_call(pinned_read, _call(), attended=True, agent_rule=ask).outcome == "ask"


@pytest.mark.parametrize(
    ("global_rule", "channel_rule", "expected"),
    [
        ("allow", "allow", "allow"),
        ("allow", "ask", "ask"),
        ("allow", "deny", "deny"),
        ("ask", "allow", "ask"),
        ("ask", "deny", "deny"),
        ("deny", "allow", "deny"),
        ("deny", "ask", "deny"),
    ],
)
def test_stricter_rule_wins(global_rule: str, channel_rule: str, expected: str) -> None:
    merged = stricter_agent_tool_rule(
        ResolvedAgentToolRule.model_validate({"rule": global_rule}),
        ResolvedAgentToolRule.model_validate({"rule": channel_rule}),
    )
    assert merged is not None and merged.rule == expected


def test_rule_channel_is_parent_and_dm_is_excluded() -> None:
    assert (
        channel_for_tool_rules(platform="discord", channel_id="parent", private_dm_id=None)
        == "parent"
    )
    assert (
        channel_for_tool_rules(platform="slack", channel_id="C123:1234.5678", private_dm_id=None)
        == "C123"
    )
    assert (
        channel_for_tool_rules(platform="discord", channel_id="source", private_dm_id="dm") is None
    )


def test_nullable_global_key_has_unique_index() -> None:
    indexes: dict[str, Index] = {
        index.name: index
        for index in cast(Table, AgentToolRuleRecord.__table__).indexes
        if index.name is not None
    }
    assert indexes["uq_agent_tool_rules_global"].unique
    assert str(indexes["uq_agent_tool_rules_global"].dialect_options["postgresql"]["where"]) == (
        "channel_id IS NULL"
    )
    assert indexes["uq_agent_tool_rules_channel"].unique


@pytest.mark.asyncio
async def test_live_lookup_sees_clear_on_next_call() -> None:
    # Replays ToolRulesSnapshot: same decider, rule cleared between calls.
    current: ResolvedAgentToolRule | None = ResolvedAgentToolRule(rule="allow")
    lookups = 0

    async def lookup(_call: ToolCall) -> ResolvedAgentToolRule | None:
        nonlocal lookups
        lookups += 1
        return current

    decider = interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        rules_for=lookup,
    )
    first = await decider(_call())
    assert first.allow
    current = None
    second = await decider(_call())
    assert not second.allow
    assert lookups == 2


@pytest.mark.asyncio
async def test_lookup_error_asks_in_chat_and_refuses_unattended() -> None:
    async def failed(_call: ToolCall) -> ResolvedAgentToolRule | None:
        raise RuntimeError("database down")

    policy = ToolSafetyPolicy(enabled=True, agent_rules=True)
    asked: list[ConfirmationPrompt] = []

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        asked.append(prompt)
        return "denied"

    chat = interactive_decider(
        policy, requester_platform_user_id="human", rules_for=failed, confirm=confirm
    )
    routine = unattended_decider(policy, rules_for=failed)
    assert not (await chat(_call("list_items"))).allow
    assert len(asked) == 1
    assert not (await routine(_call("list_items"))).allow


async def test_deny_set_while_card_waits_refuses_approved_call() -> None:
    """An explicit deny committed during the card wait wins before release."""
    current: ResolvedAgentToolRule | None = None
    reads = 0

    async def lookup(_call: ToolCall) -> ResolvedAgentToolRule | None:
        nonlocal reads
        reads += 1
        return current

    async def confirm(_prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        nonlocal current
        current = ResolvedAgentToolRule(rule="deny")
        return "approved"

    decide = interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        rules_for=lookup,
        confirm=confirm,
    )
    result = await decide(_call())
    assert not result.allow and "denies remote/append_note" in (result.deny_message or "")
    assert reads == 2


async def test_always_button_only_for_classification_asks() -> None:
    prompts: list[ConfirmationPrompt] = []
    reference = ToolRuleCardReference(
        tenant_id=uuid.uuid4(),
        agent_id="ma-1",
        agent_name="Helper",
        tool_key="remote/append_note",
        server_url="https://example.test/mcp",
    )

    async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "denied"

    async def rule(_call: ToolCall) -> ResolvedAgentToolRule | None:
        return ResolvedAgentToolRule(rule="ask")

    def card_for(_tool: ToolCall) -> ToolRuleCardReference:
        return reference

    await interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
        rules_for=rule,
    )(_call("list_items"))
    assert prompts[-1].tool_rule is None and prompts[-1].tool_rules_enabled

    async def failed_rule(_tool: ToolCall) -> ResolvedAgentToolRule | None:
        raise RuntimeError("database unavailable")

    await interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
        rules_for=failed_rule,
    )(_call("list_items"))
    assert prompts[-1].tool_rule is None
    await interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
    )(_call())
    assert prompts[-1].tool_rule == reference
    await interactive_decider(
        ToolSafetyPolicy(enabled=True),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
    )(_call())
    assert prompts[-1].tool_rule is None and not prompts[-1].tool_rules_enabled
    await interactive_decider(
        ToolSafetyPolicy(enabled=True, agent_rules=True),
        trusted_servers=frozenset({"daimon-mcp"}),
        confirmed_tools=frozenset({"add_skill"}),
        requester_platform_user_id="human",
        confirm=confirm,
        rule_card_for=card_for,
    )(_call("add_skill", "daimon-mcp"))
    assert prompts[-1].tool_rule is None


def test_only_human_admin_may_edit() -> None:
    policy = TenantAccessPolicy()
    assert authorize(policy, subject=Subject(is_admin=True), action=Action.SET_AGENT_TOOL_RULE)
    assert not authorize(policy, subject=Subject(is_admin=False), action=Action.SET_AGENT_TOOL_RULE)
    assert not authorize(
        policy,
        subject=Subject(is_admin=True, via_agent_key=True),
        action=Action.SET_AGENT_TOOL_RULE,
    )
    member = Subject(platform_user_id="member")
    assert not authorize(
        policy,
        subject=member,
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id="home"),
    )
    assert not authorize(
        policy,
        subject=member,
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id="other"),
    )
    assert not authorize(
        policy,
        subject=member,
        action=Action.SET_AGENT_TOOL_RULE,
    )
    assert not authorize(
        policy,
        subject=Subject(is_admin=True, via_agent_key=True),
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id="home"),
    )


@pytest.mark.asyncio
async def test_store_refuses_agent_key_before_touching_database() -> None:
    with pytest.raises(PermissionError):
        await store.set_rule(
            cast(AsyncSession, None),
            tenant_id=uuid.uuid4(),
            agent_id="ma-1",
            agent_name="one",
            tool_key="remote/append_note",
            server_url="https://one.example/mcp",
            rule="allow",
            set_by="agent",
            actor=Subject(is_admin=True, via_agent_key=True),
        )
    with pytest.raises(PermissionError):
        await store.set_rule(
            cast(AsyncSession, None),
            tenant_id=uuid.uuid4(),
            agent_id="ma-1",
            agent_name="one",
            tool_key="remote/append_note",
            channel_id="other",
            server_url="https://one.example/mcp",
            rule="allow",
            set_by="member",
            actor=Subject(platform_user_id="member"),
        )
    with pytest.raises(PermissionError):
        await store.set_rule(
            cast(AsyncSession, None),
            tenant_id=uuid.uuid4(),
            agent_id="ma-1",
            agent_name="one",
            tool_key="remote/append_note",
            server_url="https://one.example/mcp",
            rule="allow",
            set_by="member",
            actor=Subject(platform_user_id="member"),
        )


@pytest.mark.asyncio
async def test_store_binding_history_and_clear_cas(db_session: AsyncSession) -> None:
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    other = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add_all([tenant, other])
    await db_session.flush()
    await db_session.refresh(tenant)
    await db_session.refresh(other)
    key = "remote/append_note"
    row = await store.set_rule(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        agent_name="one",
        tool_key=key,
        server_url="https://one.example/mcp",
        rule="allow",
        set_by="admin",
        actor=Subject(is_admin=True),
        expected_revision=0,
    )
    assert row.revision == 1
    assert await store.get_for_call(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        tool_key=key,
        server_url="https://one.example/mcp",
    ) == ResolvedAgentToolRule(rule="allow")
    scoped = await store.set_rule(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        agent_name="one",
        tool_key=key,
        channel_id="home",
        server_url="https://one.example/mcp",
        rule="deny",
        set_by="admin",
        actor=Subject(is_admin=True),
        expected_revision=0,
    )
    assert scoped.revision == 1 and scoped.channel_id == "home"
    assert await store.get_for_call(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        tool_key=key,
        server_url="https://one.example/mcp",
        channel_id=channel_for_tool_rules(
            platform="discord", channel_id="home", private_dm_id=None
        ),
    ) == ResolvedAgentToolRule(rule="deny")
    assert await store.get_for_call(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        tool_key=key,
        server_url="https://one.example/mcp",
        channel_id="other",
    ) == ResolvedAgentToolRule(rule="allow")
    for tenant_id, agent_id, url in (
        (other.id, "ma-1", "https://one.example/mcp"),
        (tenant.id, "ma-2", "https://one.example/mcp"),
        (tenant.id, "ma-1", "https://two.example/mcp"),
    ):
        assert (
            await store.get_for_call(
                db_session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                tool_key=key,
                server_url=url,
            )
            is None
        )
    cleared = await store.clear_rule(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        agent_name="one",
        tool_key=key,
        server_url="https://one.example/mcp",
        set_by="admin",
        actor=Subject(is_admin=True),
        expected_revision=1,
    )
    assert cleared.revision == 2 and cleared.rule is None
    with pytest.raises(store.StaleAgentToolRule):
        await store.set_rule(
            db_session,
            tenant_id=tenant.id,
            agent_id="ma-1",
            agent_name="one",
            tool_key=key,
            server_url="https://one.example/mcp",
            rule="allow",
            set_by="admin",
            actor=Subject(is_admin=True),
            expected_revision=1,
        )
    history = (
        await db_session.scalars(
            select(AgentToolRuleHistoryRecord).order_by(AgentToolRuleHistoryRecord.id)
        )
    ).all()
    assert [(r.channel_id, r.before_rule, r.after_rule) for r in history] == [
        (None, None, "allow"),
        ("home", None, "deny"),
        (None, "allow", None),
    ]
    assert history[0].before_server_url is None
    assert history[2].before_server_url == "https://one.example/mcp"


@pytest.mark.asyncio
async def test_deny_rules_for_trusted_tool_returns_only_agent_wide_denies_of_the_tenant(
    db_session: AsyncSession,
) -> None:
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    other = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add_all([tenant, other])
    await db_session.flush()
    key = "daimon-mcp/post_update"
    url = "https://mcp.example/mcp"

    async def put(
        tenant_id: uuid.UUID,
        agent: str,
        rule: store.Rule | None,
        *,
        tool_key: str = key,
        channel_id: str | None = None,
    ) -> None:
        await store.set_rule(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent,
            agent_name=agent,
            tool_key=tool_key,
            channel_id=channel_id,
            server_url=url,
            rule=rule,
            scope="chat_and_routines",
            set_by="admin",
            actor=Subject(is_admin=True),
        )

    await put(tenant.id, "ma-deny", "deny")
    await put(tenant.id, "ma-other-agent", "deny")
    await put(tenant.id, "ma-allow", "allow")
    await put(tenant.id, "ma-ask", "ask")
    await put(tenant.id, "ma-cleared", "deny")
    await put(tenant.id, "ma-cleared", None)
    await put(tenant.id, "ma-channel", "deny", channel_id="home")
    await put(tenant.id, "ma-other-tool", "deny", tool_key="daimon-mcp/list_things")
    await put(other.id, "ma-foreign", "deny")

    rows = await store.deny_rules_for_trusted_tool(db_session, tenant_id=tenant.id, tool_key=key)

    assert sorted(row.agent_id for row in rows) == ["ma-deny", "ma-other-agent"]
    assert {(row.rule, row.channel_id, row.server_url, row.scope) for row in rows} == {
        ("deny", None, url, "chat_and_routines")
    }
    foreign = await store.deny_rules_for_trusted_tool(db_session, tenant_id=other.id, tool_key=key)
    assert [row.agent_id for row in foreign] == ["ma-foreign"]
