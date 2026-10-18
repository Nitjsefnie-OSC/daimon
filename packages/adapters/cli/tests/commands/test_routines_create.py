"""Operator creation binds the only recipient to a live tenant owner."""

from io import StringIO
from types import SimpleNamespace

import pytest
from daimon.adapters.cli.commands.routines import run_create_dm_routine
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.errors import UserFacingError
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.routines import list_routines_for_tenant
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response
from rich.console import Console

from ..harness import build_cli_runtime


@pytest.mark.parametrize("dry_run", [True, False])
async def test_create_dm_verifies_owner_and_dry_run_does_not_write(db_session_factory, dry_run):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="424242")
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    agent = ma_agent(
        id="ag",
        name="daimon",
        metadata={
            MA_METADATA_KEY_NAME: "daimon",
            MA_METADATA_KEY_TENANT: str(tenant.id),
        },
    )
    router = MARouter()
    router.add(
        "GET", r"/v1/agents", lambda _req, _m: list_response([agent.model_dump(mode="json")])
    )
    rt = build_cli_runtime(
        db_session_factory, router=router, settings=SimpleNamespace(direct_message_policies={})
    )
    calls = []

    async def member(tenant_id, guild_id, user_id):
        calls.append((tenant_id, guild_id, user_id))
        return Membership(is_member=True)

    output = StringIO()
    await run_create_dm_routine(
        rt=rt,
        console=Console(file=output),
        tenant_id=tenant.id,
        owner="123",
        agent_name="daimon",
        schedule="0 9 * * *",
        timezone="UTC",
        trigger="summarize",
        source_channel=None,
        dry_run=dry_run,
        resolver=CachedMembershipResolver(member),
    )
    async with db_session_factory() as session:
        rows = await list_routines_for_tenant(session, tenant_id=tenant.id)
    assert len(rows) == (0 if dry_run else 1)
    assert calls == [(tenant.id, "424242", "123")]
    assert "Owner / recipient: 123; budget source: tenant/account" in output.getvalue()
    if rows:
        assert rows[0].destination_id == rows[0].created_by_user_id == "123"


async def test_create_dm_unknown_membership_fails_closed(db_session_factory):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="424242")
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    rt = build_cli_runtime(db_session_factory, settings=SimpleNamespace(direct_message_policies={}))

    async def unknown(*args):
        return Membership()

    with pytest.raises(UserFacingError, match="verified"):
        await run_create_dm_routine(
            rt=rt,
            console=Console(file=StringIO()),
            tenant_id=tenant.id,
            owner="123",
            agent_name="daimon",
            schedule="0 9 * * *",
            timezone="UTC",
            trigger="summarize",
            source_channel=None,
            dry_run=True,
            resolver=CachedMembershipResolver(unknown),
        )
