"""Real product writes/readback on lane Postgres; provider I/O is scripted."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_backend import current_backend
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores import channel_admins, channel_budgets, routines, tenant_ledger, tenants
from daimon.core.stores.access_policy import load_access_policy
from daimon.testing.admin_bindings import (
    AdminBindings,
    AdminCommand,
    AdminReceipt,
    UnboundAdmin,
    parse_admin,
    provision_admin_run,
)
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.ids import ChannelRef, Provider, Scope
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["daimon.testing.db"]

FIXTURE = Path(__file__).parent.parent / "judge" / "fixtures" / "admin_operations.json"


@pytest.fixture(params=["anthropic", "openai", "gemini"])
async def binding(
    request: pytest.FixtureRequest, db_session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AdminBindings]:
    backend = cast(Provider, request.param)
    bound = await provision_admin_run(
        db_session_factory,
        workspace_id="123456789012345678",
        backend=backend,
        channels=frozenset({"223456789012345678", "323456789012345678"}),
        trace=ScriptedTransport(),
    )
    try:
        yield bound
    finally:
        await bound.close()


def source(command: str) -> dict[str, Any]:
    return {"do": "admin", "tool": "cli", "args": command}


async def invoke(binding: AdminBindings, command: str) -> AdminReceipt:
    return await binding.execute(
        source(command),
        {
            "guild_id": binding.workspace_id,
            "channel_id": min(binding.channels),
            "env.qa_user_id": "423456789012345678",
        },
    )


async def test_product_provisioning_and_configuration_are_idempotent(
    binding: AdminBindings,
) -> None:
    channel_id = min(binding.channels)
    first = await binding.configure_channel(channel_id)
    again = await binding.configure_channel(channel_id)
    assert first == again and first.observations["revision"] == 1
    tenant_id = binding.scope.tenant_id
    second = await provision_tenant(
        binding.sessions, platform="discord", workspace_id=binding.workspace_id
    )
    assert str(second.tenant_id) == tenant_id and str(second.account_id) == binding.scope.account_id
    async with binding.sessions() as session:
        revision = await current_backend(
            session, ChannelRef(tenant_id=tenant_id, platform="discord", channel_id=channel_id)
        )
    assert revision is not None and revision.backend == binding.backend
    assert not binding.trace.requests


async def test_writers_none_is_real_admission_policy_and_teardown_restores_it(
    binding: AdminBindings,
) -> None:
    receipt = await invoke(
        binding, "daimon channels rule set discord {guild_id} {channel_id} --writers none"
    )
    assert receipt.status == "executed" and receipt.observations["writers"] == "none"
    async with binding.sessions() as session:
        policy = await load_access_policy(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
    decision = authorize(
        policy,
        subject=Subject(is_admin=True),
        action=Action.START_TURN,
        place=Place(channel_id=min(binding.channels)),
    )
    assert not decision and decision.reason == "writers_none"
    await invoke(binding, "daimon channels rule set discord {guild_id} {channel_id} --writers any")
    async with binding.sessions() as session:
        restored = await load_access_policy(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
    assert authorize(
        restored,
        subject=Subject(is_admin=True),
        action=Action.START_TURN,
        place=Place(channel_id=min(binding.channels)),
    )
    assert not binding.trace.requests


async def test_real_grant_changes_product_authorization(binding: AdminBindings) -> None:
    receipt = await invoke(
        binding,
        "daimon channels admins set discord {guild_id} {channel_id} --user {env.qa_user_id}",
    )
    assert receipt.status == "executed"
    async with binding.sessions() as session:
        tenant_id = uuid.UUID(binding.scope.tenant_id)
        grant = await channel_admins.get_channel_admins(
            session, tenant_id=tenant_id, platform="discord", channel_id=min(binding.channels)
        )
        administered = await channel_admins.list_administered_channel_ids(
            session,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id="423456789012345678",
            role_ids=(),
        )
    assert grant is not None and grant.updated_by_account_id == uuid.UUID(binding.scope.account_id)
    assert administered == {min(binding.channels)}
    assert not binding.trace.requests


async def test_budget_write_and_clear_have_real_readback(binding: AdminBindings) -> None:
    receipt = await invoke(
        binding, "daimon channels budget set discord {guild_id} {channel_id} 0.01 --window total"
    )
    assert receipt.status == "executed"
    async with binding.sessions() as session:
        budget = await channel_budgets.get_channel_budget(
            session,
            tenant_id=uuid.UUID(binding.scope.tenant_id),
            platform="discord",
            channel_id=min(binding.channels),
        )
    assert budget is not None and budget.limit_usd == Decimal("0.01") and budget.window == "total"
    removed = await invoke(binding, "daimon channels budget clear discord {guild_id} {channel_id}")
    assert removed.observations == {"budget": None}
    assert not binding.trace.requests


async def test_turn_cap_restore_uses_product_override(binding: AdminBindings) -> None:
    receipt = await invoke(binding, "daimon tenants turn-cap discord {guild_id} 2")
    assert receipt.observations == {"turn_cap": 2}
    assert (
        await tenants.get_turn_cap(
            binding.sessions, tenant_id=uuid.UUID(binding.scope.tenant_id), default=8
        )
        == 2
    )
    restored = await invoke(binding, "daimon tenants turn-cap discord {guild_id} default")
    assert restored.observations == {"turn_cap": None}
    assert (
        await tenants.get_turn_cap(
            binding.sessions, tenant_id=uuid.UUID(binding.scope.tenant_id), default=8
        )
        == 8
    )


async def test_routine_teardown_only_removes_owned_channel_and_tenant(
    binding: AdminBindings,
) -> None:
    tenant_id = uuid.UUID(binding.scope.tenant_id)
    target, other_channel = sorted(binding.channels)
    foreign = await provision_tenant(
        binding.sessions, platform="discord", workspace_id="523456789012345678"
    )
    async with binding.sessions() as session, session.begin():
        owned = await routines.create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=None,
            agent_id="fixture-native-agent",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="fixture reminder",
            channel_id=target,
        )
        other = await routines.create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=None,
            agent_id="fixture-native-agent",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="fixture reminder",
            channel_id=other_channel,
        )
        outside = await routines.create_routine(
            session,
            tenant_id=foreign.tenant_id,
            created_by_user_id=None,
            agent_id="fixture-native-agent",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="fixture reminder",
            channel_id=target,
        )
    receipt = await binding.execute(
        {"do": "admin", "tool": "delete_routines", "args": {"channel": "{channel_id}"}},
        {"channel_id": target},
    )
    assert receipt.status == "executed" and receipt.observations["deleted"] == [str(owned.id)]
    async with binding.sessions() as session:
        assert [
            r.id for r in await routines.list_routines_for_tenant(session, tenant_id=tenant_id)
        ] == [other.id]
        assert [
            r.id
            for r in await routines.list_routines_for_tenant(session, tenant_id=foreign.tenant_id)
        ] == [outside.id]
    again = await binding.execute(
        {"do": "admin", "tool": "delete_routines", "args": {"channel": target}}, {}
    )
    assert again.observations["deleted"] == []


@pytest.mark.parametrize(
    "command",
    [
        "daimon channels rule set discord {guild_id} {channel_id} --readers own --copy-from daimon",
        "daimon channels skills add discord {guild_id} {channel_id} notes",
        "daimon skills --guild {guild_id} delete qa-echo-skill --yes",
    ],
)
async def test_missing_resource_edge_is_typed_pending_without_io(
    binding: AdminBindings, command: str
) -> None:
    receipt = await invoke(binding, command)
    assert receipt.status == "pending" and receipt.code == "ADMIN_RESOURCE_EDGE_UNBOUND"
    assert not binding.trace.requests


async def test_frozen_credit_draining_does_not_forge_a_ledger_write(binding: AdminBindings) -> None:
    receipt = await invoke(
        binding,
        "daimon tenants credit discord {guild_id} -<balance> --note qa-deplete --id qa-deplete-fixture",
    )
    assert receipt.status == "pending" and receipt.code == "ADMIN_CREDIT_SOURCE_NOT_EXECUTABLE"
    async with binding.sessions() as session:
        assert (
            await tenant_ledger.get_balance(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
            == 0
        )
    assert not binding.trace.requests


@pytest.mark.parametrize(
    "command",
    [
        "daimon channels admins set discord foreign {channel_id} --user 423456789012345678",
        "daimon channels admins set discord {guild_id} 923456789012345678 --user 423456789012345678",
        "daimon tenants turn-cap slack {guild_id} 2",
    ],
)
async def test_foreign_destination_is_refused_before_mutation(
    binding: AdminBindings, command: str
) -> None:
    with pytest.raises(ScopeViolation):
        await invoke(binding, command)
    assert not binding.trace.requests


async def test_foreign_account_and_unscoped_context_are_refused(binding: AdminBindings) -> None:
    foreign = await provision_tenant(
        binding.sessions, platform="discord", workspace_id="623456789012345678"
    )
    for scope in (
        binding.scope.model_copy(update={"account_id": str(foreign.account_id)}),
        Scope.platform(reason="forbidden qa"),
    ):
        refused = replace(binding, scope=scope)
        try:
            with pytest.raises(ScopeViolation):
                await refused.configure_channel(min(binding.channels))
        finally:
            await refused.close()
    assert not binding.trace.requests


@pytest.mark.parametrize(
    "command",
    [
        "daimon channels admins set discord {guild_id} {channel_id} --user {missing}",
        "daimon channels admins set discord {guild_id} {channel_id} --header SECRET",
        "daimon channels admins set discord {guild_id} {channel_id} --user u1 --user u2",
        "daimon channels rule set discord {guild_id} {channel_id} --writers any ; rm -rf /",
    ],
)
async def test_unknown_syntax_remains_pending_no_shell_or_io(
    binding: AdminBindings, command: str
) -> None:
    receipt = await invoke(binding, command)
    assert receipt.status == "pending"
    assert not binding.trace.requests


async def test_fixture_value_cannot_inject_additional_shell_tokens(binding: AdminBindings) -> None:
    with pytest.raises(ValueError):
        await binding.execute(
            source(
                "daimon channels admins set discord {guild_id} {channel_id} --user {env.qa_user_id}"
            ),
            {
                "channel_id": min(binding.channels),
                "env.qa_user_id": "423456789012345678 --user other; curl stolen.invalid",
            },
        )
    assert not binding.trace.requests


async def test_native_platform_permission_and_token_steps_remain_pending(
    binding: AdminBindings,
) -> None:
    for step in (
        {
            "do": "admin",
            "tool": "discord_overwrite",
            "args": {"target": "daimon_bot", "deny": ["CREATE_PUBLIC_THREADS"]},
        },
        {
            "do": "admin",
            "tool": "headless",
            "args": {
                "call": "credential_submit.mcp_token",
                "token": "wrong-token-123",
                "url": "https://fixture.invalid",
            },
        },
    ):
        receipt = await binding.execute(step, {})
        assert receipt.status == "pending" and not receipt.observations
    assert not binding.trace.requests


def test_frozen_admin_source_pin_and_operation_coverage() -> None:
    snapshot = json.loads(FIXTURE.read_text())
    assert (
        snapshot["target_sha256"]
        == "fc14eab684b6aa257ca5c01ab113ef154c9847938d263f2739480299c43cc2f4"
    )
    assert len(snapshot["scenarios"]) == 19
    values = {
        "guild_id": "123456789012345678",
        "channel_id": "223456789012345678",
        "channel:A": "223456789012345678",
        "channel:B": "323456789012345678",
        "env.qa_user_id": "423456789012345678",
        "env.qa_drain_guild_id": "123456789012345678",
        "env.qa_library_skill": "library",
        "env.qa_base_with_own_skill": "skill-source",
        "nonce": "fixture",
        "fork.name": "fork",
    }
    pending: list[tuple[str, str]] = []
    parsed: list[AdminCommand] = []
    for row in snapshot["scenarios"]:
        assert len(row["source_sha256"]) == 64
        for invocation in row["operations"]:
            try:
                parsed.append(parse_admin(invocation["source"], values))
            except UnboundAdmin as error:
                pending.append((row["scenario_id"], error.code))
    assert len(parsed) == 36 and pending == [
        ("QA-NEW14-MISSING-THREAD-PERMISSION-COPY", "ADMIN_PLATFORM_EDGE_UNBOUND"),
        ("QA-NEW27-MISTYPED-TOKEN-HEADLESS", "ADMIN_TOOL_UNBOUND"),
    ]
    assert all("{" not in (command.channel_id or "") for command in parsed)


async def test_named_channel_reference_preserves_owned_destination(binding: AdminBindings) -> None:
    target = max(binding.channels)
    receipt = await binding.execute(
        source("daimon channels rule set discord {guild_id} {channel:A} --writers none"),
        {"channel:A": target},
    )
    assert receipt.status == "executed" and receipt.observations["writers"] == "none"
    async with binding.sessions() as session:
        policy = await load_access_policy(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
    assert policy.channel_rules[target].writers == "none"
    assert min(binding.channels) not in policy.channel_rules


@pytest.mark.parametrize(
    "scenario_id",
    [
        "QA-NEW13-WRITERS-NONE-NOT-SILENT",
        "QA-NEW15-CHANNEL-BUDGET-EXHAUSTED",
        "QA-NEW6-TENANT-CAP-QUEUE",
        "QA-NEW22-ROUTINE-OUTPUT-NOT-TRUNCATED",
        "QA-NEW23-ROUTINE-WITHOUT-DESTINATION",
        "QA-NEW35-ROUTINE-FAILURE-IS-REPORTED",
    ],
)
async def test_exact_frozen_common_admin_operations_execute(
    binding: AdminBindings, scenario_id: str
) -> None:
    # Operation proof only: host turns/assertions are the runner's separate work.
    row = next(
        row
        for row in json.loads(FIXTURE.read_text())["scenarios"]
        if row["scenario_id"] == scenario_id
    )
    receipts = [
        await binding.execute(invocation["source"], {"channel_id": min(binding.channels)})
        for invocation in row["operations"]
    ]
    assert all(receipt.status == "executed" for receipt in receipts)
    assert all(receipt.backend == binding.backend for receipt in receipts)
    assert not binding.trace.requests


@pytest.mark.parametrize(
    "command",
    [
        "daimon channels admins set discord {guild_id} {channel_id}",
        "daimon channels rule set discord {guild_id} {channel_id} --writers unknown",
        "daimon tenants turn-cap discord {guild_id} 02",
        "daimon tenants turn-cap discord {guild_id} 0",
    ],
)
async def test_unbound_semantics_stay_pending_before_mutation(
    binding: AdminBindings, command: str
) -> None:
    receipt = await invoke(binding, command)
    assert receipt.status == "pending" and receipt.code is not None
    assert not binding.trace.requests


async def test_non_lane_database_refuses_before_any_connection() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("postgresql+asyncpg://offline:fixture@127.0.0.1/operator_database")
    sessions = async_sessionmaker(engine)
    try:
        with pytest.raises(ValueError, match="non-lane test databases"):
            await provision_admin_run(
                sessions,
                workspace_id="123",
                backend="anthropic",
                channels=frozenset(),
                trace=ScriptedTransport(),
            )
    finally:
        await engine.dispose()
