"""Routing for ask-a-human requests.

The adapter posts support requests to its configured escalation channel.
"""

from __future__ import annotations

import uuid
from typing import Final

from daimon.core.native_admins import GroupMembers, ServerAdminIds
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_RECIPIENTS: Final = 10


async def support_recipient_tiers(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    requester_id: str,
    members: GroupMembers | None = None,
    server_admins: ServerAdminIds | None = None,
) -> tuple[tuple[str, ...], ...]:
    """No DM tier: the normal escalation channel receives the request."""
    return ()


__all__ = ["MAX_RECIPIENTS", "support_recipient_tiers"]
