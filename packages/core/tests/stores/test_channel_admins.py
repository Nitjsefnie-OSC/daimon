"""Legacy grant rows remain erasable until their table is dropped."""

from __future__ import annotations

from daimon.core._models import ChannelAdmin
from daimon.core.stores.channel_admins import (
    count_channel_admin_grants_for_user,
    remove_user_from_channel_admins,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_erase_user_from_legacy_rows(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    db_session.add_all(
        [
            ChannelAdmin(
                tenant_id=tenant.id,
                platform="discord",
                channel_id="c1",
                role_ids=[],
                user_ids=["u1"],
            ),
            ChannelAdmin(
                tenant_id=tenant.id,
                platform="discord",
                channel_id="c2",
                role_ids=["r1"],
                user_ids=["u1", "u2"],
            ),
        ]
    )
    await db_session.flush()
    key = {"tenant_id": tenant.id, "platform": "discord", "platform_user_id": "u1"}
    assert await count_channel_admin_grants_for_user(db_session, **key) == 2
    assert await remove_user_from_channel_admins(db_session, **key) == 2
    rows = (
        (await db_session.execute(select(ChannelAdmin).where(ChannelAdmin.tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].channel_id == "c2"
    assert rows[0].user_ids == ["u2"]
    assert rows[0].role_ids == ["r1"]
    assert await count_channel_admin_grants_for_user(db_session, **key) == 0
