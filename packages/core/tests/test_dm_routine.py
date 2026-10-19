"""A DM routine runs as its owner's fresh DM turn: live-DM session, unattended."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from daimon.core import direct_messages
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.direct_messages import (
    DirectMessageAccessRevoked,
    admit_cold_dm,
    reply_to_dm_result,
    run_dm_routine,
    start_cold_dm,
)
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.direct_messages import (
    get_conversation,
    set_dm_agent_name,
    set_dm_enabled,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.core.turn.admission import DmSource
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import PolicyApproval
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
    make_thread_session,
)
from daimon.testing.ma import build_fake_anthropic, resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session, ma_session_agent
from daimon.testing.turn_fakes import deps_for
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

OWNER = "999"
ROUTE = "555"


class _Built(Exception):
    """Stops a turn at session creation, carrying what it would have created."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("session create reached")
        self.kwargs = kwargs


async def _seed(factory: async_sessionmaker[AsyncSession], *, member: bool | None = True):
    async with factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="700000101")
        await make_account(session, tenant=tenant)
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="private")
        await make_tenant_config(
            session, tenant=tenant, agent_name="private", environment_name="default"
        )
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("10"))
    agent = ma_agent(id="ag_private", name="private", tenant_id=tenant.id)
    environment = ma_environment(id="env_1", name="default", tenant_id=tenant.id)
    client = build_fake_anthropic(resolved_agent_env_router(agent, environment).dispatch)

    async def membership(*_: object) -> Membership:
        return Membership(is_member=member)

    deps = replace(
        deps_for(client, factory),
        membership_resolver=CachedMembershipResolver(membership),
    )
    return tenant, deps, client


def _capture_create(monkeypatch: pytest.MonkeyPatch) -> None:
    async def create_session(*args: object, **kwargs: Any) -> object:
        raise _Built(kwargs)

    monkeypatch.setattr("daimon.core.turn.prepare.create_session", create_session)


async def _live_dm_session(deps, tenant) -> dict[str, Any]:
    """What the first turn of a fresh live cold DM would create."""
    admission = await admit_cold_dm(
        deps,
        tenant_id=tenant.id,
        external_user_id=OWNER,
        channel_id=ROUTE,
        role=Role.USER,
        role_ids=(),
        verified_membership=True,
    )
    row = await start_cold_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key=ROUTE,
        external_user_id=OWNER,
    )
    assert row is not None
    with pytest.raises(_Built) as built:
        await reply_to_dm_result(
            deps,
            platform="discord",
            route_key=ROUTE,
            external_user_id=OWNER,
            message_id="m1",
            expected_scope_id=row.scope_id,
            text="hi",
            role=Role.USER,
            verified_membership=True,
        )
    return built.value.kwargs


async def _routine_session(deps, tenant, **overrides: Any) -> dict[str, Any]:
    with pytest.raises(_Built) as built:
        await run_dm_routine(
            deps,
            tenant_id=tenant.id,
            owner_user_id=OWNER,
            agent_name=overrides.pop("agent_name", "private"),
            prompt="summarize my week",
            source=overrides.pop("source", None),
            budget_channel_id=overrides.pop("budget_channel_id", None),
        )
    return built.value.kwargs


# Per-turn placement and hooks; everything else must match a live DM's.
_PLACEMENT = {
    "private_dm_id",
    "origin_channel_id",
    "origin_thread_id",
    "origin_seal_ids",
    "budget_channel_id",
    "before_create",
    "on_app_access",
    "allocation",
}


def _normalized(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key not in _PLACEMENT}


@pytest.mark.parametrize("read_only", [False, True], ids=["writable", "dm_memory_read_only"])
async def test_routine_session_equals_a_fresh_live_dm_with_personal_memory(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    tenant, deps, client = await _seed(db_session_factory)
    if read_only:
        async with db_session_factory.begin() as session:
            await set_access_policy(
                session, tenant_id=tenant.id, policy=TenantAccessPolicy(dm_memory_read_only=True)
            )
    _capture_create(monkeypatch)

    live = await _live_dm_session(deps, tenant)
    routine = await _routine_session(deps, tenant)
    await client.close()

    assert routine["personal_memory"] is True, "the owner's personal memory is mounted"
    assert routine["memory_read_only"] is read_only, "the DM memory policy, no routine override"
    assert routine["account_id"] == live["account_id"] is not None, "the owner's own account"
    assert _normalized(routine) == _normalized(live), "same agent, tools, resources and memory"
    assert routine["private_dm_id"].startswith("dm:"), "a private scope of its own"
    assert routine["private_dm_id"] != live["private_dm_id"]


async def test_routine_never_touches_the_owners_selected_cold_dm(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C8: the owner's active conversation, its history and session stay as they were."""
    from daimon.core.routine_sessions import RoutineFire

    async def completed(self: RoutineFire, session_id: str) -> None:
        pass

    # This admission/route unit test stubs binding and execution. Durable
    # allocation and stopping are covered by the scheduler transport tests.
    monkeypatch.setattr(RoutineFire, "completed", completed)
    tenant, deps, client = await _seed(db_session_factory)
    admission = await admit_cold_dm(
        deps,
        tenant_id=tenant.id,
        external_user_id=OWNER,
        channel_id=ROUTE,
        role=Role.USER,
        role_ids=(),
        verified_membership=True,
    )
    active = await start_cold_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key=ROUTE,
        external_user_id=OWNER,
    )
    assert active is not None
    async with db_session_factory.begin() as session:
        live = await make_thread_session(
            session,
            tenant=tenant,
            thread_id=active.scope_id,
            ma_session_id="live_private_session",
            account=await get_account(session, admission.account_id),
        )
        before = await get_conversation(
            session, platform="discord", route_key=ROUTE, external_user_id=OWNER
        )
    seen: dict[str, Any] = {}

    async def bind(deps_: object, admission_: Any, **kwargs: Any) -> PreparedTurn:
        seen["bind"] = kwargs
        seen["admission"] = admission_
        return PreparedTurn(
            admission=admission_,
            ma_session_id="routine_session",
            mapping_id=None,
            watermark=None,
            reused=False,
            session_account_id=kwargs["session_account_id"],
            _record=None,  # type: ignore[arg-type]
        )

    async def run(deps_: object, prepared: PreparedTurn, **kwargs: Any) -> RunOutcome:
        seen["run"] = kwargs
        from daimon.core.turn.state import TextBlock

        return RunOutcome(
            state=TurnState(
                content=[TextBlock(kind="text", text="Your week: all green.")],
                termination=TerminationReason.COMPLETED,
            ),
            ma_session_id=prepared.ma_session_id,
            mapping_id=None,
            recovered=False,
        )

    async def gate(*args: object, **kwargs: object) -> None:
        seen["gated"] = True

    async def retrieve(*args: object, **kwargs: object) -> object:
        return ma_session(id="routine_session", agent=ma_session_agent(id="ag_private"))

    monkeypatch.setattr(direct_messages, "bind_session", bind)
    monkeypatch.setattr(direct_messages, "run_prepared_turn", run)
    monkeypatch.setattr(direct_messages, "gate_session_snapshot", gate)
    monkeypatch.setattr(direct_messages, "retrieve_session_record", retrieve)

    reply = await run_dm_routine(
        deps,
        tenant_id=tenant.id,
        owner_user_id=OWNER,
        agent_name="private",
        prompt="summarize my week",
        source=None,
        budget_channel_id="budget-chan",
    )
    await client.close()

    assert reply.text == "Your week: all green." and reply.session_id == "routine_session"
    scope = seen["bind"]["thread_id"]
    assert scope.startswith("dm:") and scope != active.scope_id
    assert seen["bind"]["reuse_existing"] is False, "never another fire's or a live session"
    assert seen["admission"].private_dm_id == scope
    assert seen["admission"].grant.dm_source is None, "source-free stays source-free"
    assert seen["admission"].budget_channel_id == "budget-chan", "billing only"
    run_kwargs = seen["run"]
    assert run_kwargs["origin"] == "routine" and run_kwargs["attended"] is False
    assert isinstance(run_kwargs["tool_confirmation"], PolicyApproval)
    assert seen["gated"], "the session is gated before anything is sent"
    assert run_kwargs["thread_id"] == scope
    assert "summarize my week" in run_kwargs["user_message"]
    assert "private conversation" not in run_kwargs["user_message"], "no DM history"
    async with db_session_factory() as session:
        after = await get_conversation(
            session, platform="discord", route_key=ROUTE, external_user_id=OWNER
        )
        mapping = await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=active.scope_id,
            account_id=admission.account_id,
        )
        routine_binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=ROUTE,
            thread_id=scope,
        )
    assert after == before, "the selected conversation, its history and claim are unchanged"
    assert mapping is not None and mapping.id == live.id, "the live session is untouched"
    assert routine_binding is None, "no responder binding on the physical DM route"


async def test_routine_creates_no_conversation_when_none_was_selected(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, deps, client = await _seed(db_session_factory)
    _capture_create(monkeypatch)
    await _routine_session(deps, tenant)
    await client.close()
    async with db_session_factory() as session:
        assert (
            await get_conversation(
                session, platform="discord", route_key=ROUTE, external_user_id=OWNER
            )
            is None
        )


@pytest.mark.parametrize("mode", ["dm_disabled", "revoked", "unknown_membership"])
async def test_routine_refuses_without_live_private_access(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    tenant, deps, client = await _seed(
        db_session_factory,
        member={"revoked": False, "unknown_membership": None}.get(mode, True),
    )
    if mode == "dm_disabled":
        async with db_session_factory.begin() as session:
            await set_dm_enabled(session, tenant_id=tenant.id, enabled=False)
    _capture_create(monkeypatch)
    with pytest.raises((AdmissionDenied, DirectMessageAccessRevoked)):
        await run_dm_routine(
            deps,
            tenant_id=tenant.id,
            owner_user_id=OWNER,
            agent_name="private",
            prompt="p",
            source=None,
            budget_channel_id=None,
        )
    await client.close()


async def test_routine_refuses_an_agent_its_owner_may_not_dm_with(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold-DM agent rules apply to the saved agent, without an admin exemption."""
    tenant, deps, client = await _seed(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(agent_rules={"private": AgentRule(runs_in=("chan-1",))}),
        )
    _capture_create(monkeypatch)
    with pytest.raises(AdmissionDenied):
        await _routine_session(deps, tenant)
    await client.close()


@pytest.mark.parametrize("sealed", ["channel", "thread"])
async def test_sealed_creation_source_refuses_before_a_session_exists(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    sealed: str,
) -> None:
    tenant, deps, client = await _seed(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                channel_rules={
                    ("789" if sealed == "thread" else "456"): ChannelRule(readers="inside")
                }
            ),
        )
    _capture_create(monkeypatch)
    with pytest.raises(DirectMessageAccessRevoked) as refused:
        await run_dm_routine(
            deps,
            tenant_id=tenant.id,
            owner_user_id=OWNER,
            agent_name="private",
            prompt="p",
            source=DmSource(channel_id="456", thread_id="789"),
            budget_channel_id="456",
        )
    await client.close()
    assert refused.value.reason == "source_sealed"


def _slack_call() -> ToolCall:
    return ToolCall(
        tool_use_id="tu_1",
        server_name="slack",
        tool_name="send_message",
        input={"channel": "#general", "text": "my personal notes"},
    )


@pytest.mark.parametrize(
    ("policy", "allowed"),
    [
        (ToolSafetyPolicy(enabled=True), False),
        (ToolSafetyPolicy(enabled=True, unattended_writes=("slack/send_message",)), True),
    ],
    ids=["refused", "allow_listed"],
)
async def test_a_public_send_in_the_run_is_decided_by_the_unattended_gate(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    policy: ToolSafetyPolicy,
    allowed: bool,
) -> None:
    """Nobody is watching: a third-party send needs the unattended allow-list."""
    tenant, deps, client = await _seed(db_session_factory)
    deps = replace(deps, tool_safety=policy)
    session = ma_session(
        id="routine_session",
        agent=ma_session_agent(
            id="ag_private",
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "slack",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            ],
            mcp_servers=[{"type": "url", "name": "slack", "url": "https://slack.example/mcp"}],
        ),
    )
    updates: list[dict[str, Any]] = []

    async def retrieve(*args: object, **kwargs: object) -> object:
        return session

    async def update(*args: object, **kwargs: Any) -> None:
        updates.append(kwargs["agent"])

    monkeypatch.setattr(direct_messages, "retrieve_session_record", retrieve)
    monkeypatch.setattr("daimon.core.turn.unattended.update_session_record", update)
    prepared = PreparedTurn(
        admission=None,  # type: ignore[arg-type]
        ma_session_id="routine_session",
        mapping_id=None,
        watermark=None,
        reused=False,
        session_account_id=uuid.uuid4(),
        _record=None,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(direct_messages, "prepared_annotation_lookup", lambda *a, **k: None)

    posture = await direct_messages._unattended_gate(  # pyright: ignore[reportPrivateUsage]
        deps, prepared, tenant_id=tenant.id, scope=Scope.legacy_host_authorized(call_site="test")
    )
    await client.close()

    assert isinstance(posture, PolicyApproval)
    (toolset,) = updates[0]["tools"]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}, (
        "the third-party server pauses, so its calls are decided"
    )
    result = await posture.decide(_slack_call())
    assert result.allow is allowed
    if not allowed:
        assert result.deny_message
