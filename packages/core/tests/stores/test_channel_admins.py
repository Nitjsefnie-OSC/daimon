"""Store tests for channel admin grants and the account's stored role ids."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.authz import Subject
from daimon.core.config import reset_channel_admins_enabled
from daimon.core.stores import agent_tool_rules
from daimon.core.stores.accounts import get_account_with_tenant, set_platform_role_ids
from daimon.core.stores.channel_admins import (
    count_channel_admin_grants_for_user,
    delete_channel_admins,
    get_channel_admins,
    list_administered_channel_ids,
    list_channel_admins,
    remove_user_from_channel_admins,
    set_channel_admins,
)
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_set_channel_admins_upserts_sorted_unique_ids(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    first = await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c1",
        role_ids=["r2", "r1", "r2"],
        user_ids=[],
        actor_account_id=None,
    )
    assert first.role_ids == ("r1", "r2"), "role ids are sorted and unique"
    second = await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c1",
        role_ids=[],
        user_ids=["u1"],
        actor_account_id=None,
    )
    assert (second.role_ids, second.user_ids) == ((), ("u1",)), "a write replaces both lists"
    rows = await list_channel_admins(db_session, tenant_id=tenant.id, platform="discord")
    assert [row.channel_id for row in rows] == ["c1"], "one row per channel"


async def test_grant_matches_user_or_role_and_stays_in_its_tenant(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c1",
        role_ids=["r1"],
        user_ids=["u1"],
        actor_account_id=None,
    )

    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c2",
        role_ids=["r2"],
        user_ids=[],
        actor_account_id=None,
    )

    async def administered(tenant_id: uuid.UUID, user: str, roles: list[str]) -> frozenset[str]:
        return await list_administered_channel_ids(
            db_session,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id=user,
            role_ids=roles,
        )

    assert await administered(tenant.id, "u1", []) == {"c1"}, "a user grant counts"
    assert await administered(tenant.id, "u9", ["r0", "r1", "r2"]) == {"c1", "c2"}, (
        "every role grant counts"
    )
    assert await administered(tenant.id, "u9", ["r0"]) == frozenset(), "an ungranted role does not"
    assert await administered(other.id, "u1", ["r1"]) == frozenset(), "never across tenants"


async def test_remove_user_drops_their_id_and_empty_rows(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    for channel, roles in (("c1", []), ("c2", ["r1"])):
        await set_channel_admins(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=channel,
            role_ids=roles,
            user_ids=["u1", "u2"] if channel == "c2" else ["u1"],
            actor_account_id=None,
        )
    key = {"tenant_id": tenant.id, "platform": "discord", "platform_user_id": "u1"}
    assert await count_channel_admin_grants_for_user(db_session, **key) == 2, (
        "both grants are counted"
    )

    assert await remove_user_from_channel_admins(db_session, **key) == 2, (
        "removing the user touches both rows"
    )
    ids = {"tenant_id": tenant.id, "platform": "discord"}
    assert await get_channel_admins(db_session, **ids, channel_id="c1") is None, (
        "a row left empty is deleted"
    )
    kept = await get_channel_admins(db_session, **ids, channel_id="c2")
    assert kept is not None and kept.user_ids == ("u2",) and kept.role_ids == ("r1",), (
        "other admins stay"
    )
    assert await delete_channel_admins(db_session, **ids, channel_id="c2"), (
        "delete reports the removed row"
    )
    assert not await delete_channel_admins(db_session, **ids, channel_id="c2"), (
        "a second delete finds nothing"
    )


async def test_platform_role_ids_round_trip_through_identity_read(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant, account=account
    )
    before = await get_account_with_tenant(db_session, account_id=account.id)
    assert before is not None and before.platform_role_ids == (), (
        "no role ids until a turn stores them"
    )

    await set_platform_role_ids(db_session, account.id, ["r2", "r1", "r1"])
    after = await get_account_with_tenant(db_session, account_id=account.id)
    assert after is not None and after.platform_role_ids == ("r1", "r2"), (
        "role ids are stored sorted and unique"
    )


async def test_disabled_grants_survive_restart_and_privacy_erasure_still_applies(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_channel_admins_enabled()
    monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "true")
    tenant = await make_tenant(db_session)
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c1",
        role_ids=[],
        user_ids=["u1", "u2"],
        actor_account_id=None,
    )
    await agent_tool_rules.set_rule(
        db_session,
        tenant_id=tenant.id,
        agent_id="ma-1",
        agent_name="Helper",
        tool_key="remote/append_note",
        channel_id="c1",
        server_url="https://example.test/mcp",
        rule="allow",
        set_by="admin",
        actor=Subject(is_admin=True),
    )
    try:
        reset_channel_admins_enabled()
        monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "false")
        assert (
            await list_administered_channel_ids(
                db_session,
                tenant_id=tenant.id,
                platform="discord",
                platform_user_id="u1",
                role_ids=(),
            )
            == frozenset()
        )
        saved = await agent_tool_rules.get_rule(
            db_session,
            tenant_id=tenant.id,
            agent_id="ma-1",
            tool_key="remote/append_note",
            channel_id="c1",
        )
        assert saved is not None and saved.rule == "allow"
        key = {"tenant_id": tenant.id, "platform": "discord", "platform_user_id": "u1"}
        assert await count_channel_admin_grants_for_user(db_session, **key) == 1
        assert await remove_user_from_channel_admins(db_session, **key) == 1
        reset_channel_admins_enabled()
        monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "true")
        assert await list_administered_channel_ids(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            platform_user_id="u2",
            role_ids=(),
        ) == {"c1"}
        assert (
            await list_administered_channel_ids(
                db_session,
                tenant_id=tenant.id,
                platform="discord",
                platform_user_id="u1",
                role_ids=(),
            )
            == frozenset()
        )
        retained = await get_channel_admins(
            db_session, tenant_id=tenant.id, platform="discord", channel_id="c1"
        )
        assert retained is not None and retained.user_ids == ("u2",)
        saved_again = await agent_tool_rules.get_rule(
            db_session,
            tenant_id=tenant.id,
            agent_id="ma-1",
            tool_key="remote/append_note",
            channel_id="c1",
        )
        assert saved_again == saved
    finally:
        reset_channel_admins_enabled()
