"""An admin's per-routine opt-in to built-in sandbox tools, and its change log."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from daimon.core.authz import Subject
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    create_routine,
    get_routine,
    sandbox_tools_history,
    set_sandbox_tools,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

ADMIN = Subject(is_admin=True, platform_user_id="admin-1")


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    return (await make_tenant(db_session)).id


async def _routine(
    session: AsyncSession, tenant_id: uuid.UUID, owner: str = "owner-1"
) -> RoutineRow:
    return await create_routine(
        session,
        tenant_id=tenant_id,
        created_by_user_id=owner,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="nightly",
    )


async def test_a_new_routine_has_sandbox_tools_off(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)
    assert row.sandbox_tools_enabled is False
    assert row.sandbox_tools_set_by is None
    assert row.sandbox_tools_set_at is None


async def test_admin_opt_in_is_recorded_with_who_when_and_routine(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)

    result = await set_sandbox_tools(
        db_session,
        row.id,
        tenant_id=tenant_id,
        enabled=True,
        set_by="account-admin",
        actor=ADMIN,
        via="mcp",
    )

    assert result is not None
    updated, changed = result
    assert changed is True
    assert updated.sandbox_tools_enabled is True
    assert updated.sandbox_tools_set_by == "account-admin"
    assert updated.sandbox_tools_set_at is not None
    [entry] = await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)
    assert (entry.routine_id, entry.before_enabled, entry.after_enabled) == (row.id, False, True)
    assert (entry.set_by, entry.via) == ("account-admin", "mcp")
    assert entry.set_at == updated.sandbox_tools_set_at


@pytest.mark.parametrize(
    "actor",
    [
        Subject(is_admin=False, platform_user_id="owner-1"),
        Subject(is_admin=True, platform_user_id="owner-1", via_agent_key=True),
    ],
    ids=["owner_not_admin", "admin_agent_key"],
)
async def test_only_a_human_admin_may_opt_in(
    db_session: AsyncSession, tenant_id: uuid.UUID, actor: Subject
) -> None:
    """Owning the routine is not enough, and an admin's agent key is not a person."""
    row = await _routine(db_session, tenant_id, owner="owner-1")

    with pytest.raises(PermissionError):
        await set_sandbox_tools(
            db_session,
            row.id,
            tenant_id=tenant_id,
            enabled=True,
            set_by="owner-account",
            actor=actor,
            via="mcp",
        )

    after = await get_routine(db_session, row.id, tenant_id=tenant_id)
    assert after is not None and after.sandbox_tools_enabled is False
    assert await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id) == []


async def test_setting_the_same_value_again_writes_nothing(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)
    for _ in range(2):
        await set_sandbox_tools(
            db_session,
            row.id,
            tenant_id=tenant_id,
            enabled=True,
            set_by="a",
            actor=ADMIN,
            via="cli",
        )
    result = await set_sandbox_tools(
        db_session, row.id, tenant_id=tenant_id, enabled=True, set_by="b", actor=ADMIN, via="cli"
    )

    assert result is not None and result[1] is False
    assert result[0].sandbox_tools_set_by == "a"
    assert len(await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)) == 1


async def test_opt_in_changes_only_that_routine(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    chosen = await _routine(db_session, tenant_id)
    other = await _routine(db_session, tenant_id)

    await set_sandbox_tools(
        db_session, chosen.id, tenant_id=tenant_id, enabled=True, set_by="a", actor=ADMIN, via="mcp"
    )

    after_other = await get_routine(db_session, other.id, tenant_id=tenant_id)
    assert after_other is not None and after_other.sandbox_tools_enabled is False


async def test_another_tenant_cannot_reach_the_routine(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)
    stranger = (await make_tenant(db_session)).id

    assert (
        await set_sandbox_tools(
            db_session, row.id, tenant_id=stranger, enabled=True, set_by="a", actor=ADMIN, via="mcp"
        )
        is None
    )
