"""Privacy cleanup for legacy channel grant rows retained until their table is dropped."""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import ChannelAdmin
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def remove_user_from_channel_admins(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    """Erase one member's user id from every channel's admins. Returns rows touched.

    A row left with no roles and no users is deleted as an empty shell.
    """
    result = await session.execute(
        update(ChannelAdmin)
        .where(
            ChannelAdmin.tenant_id == tenant_id,
            ChannelAdmin.platform == platform,
            ChannelAdmin.user_ids.contains([platform_user_id]),
        )
        .values(user_ids=func.array_remove(ChannelAdmin.user_ids, platform_user_id))
    )
    touched = cast(CursorResult[Any], result).rowcount
    await session.execute(
        delete(ChannelAdmin).where(
            ChannelAdmin.tenant_id == tenant_id,
            ChannelAdmin.platform == platform,
            func.cardinality(ChannelAdmin.user_ids) == 0,
            func.cardinality(ChannelAdmin.role_ids) == 0,
        )
    )
    await session.flush()
    return touched


async def count_channel_admin_grants_for_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    """How many channels list this member's user id: what erasure would touch."""
    stmt = (
        select(func.count())
        .select_from(ChannelAdmin)
        .where(
            ChannelAdmin.tenant_id == tenant_id,
            ChannelAdmin.platform == platform,
            ChannelAdmin.user_ids.contains([platform_user_id]),
        )
    )
    return int((await session.execute(stmt)).scalar_one())
