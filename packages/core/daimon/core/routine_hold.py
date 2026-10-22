"""Whether a routine fire into a channel shared with another organisation is held.

A routine's destination is its output's audience. When that channel is recorded
as shared (`shared_channels`), everyone in it, people from another organisation
included, reads what the routine posts. So the fire runs held to that channel
like any turn there: its memory is the channel's own store and its Daimon
credential is the held one (`daimon.core.turn_hold`). It is never run unheld.

A thread saved without its parent channel cannot be placed here; the scheduler
refuses it before any hold is decided (`routine_destination_parent_unknown`).
"""

from __future__ import annotations

from daimon.core.rule_views import routine_destination_channel
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.shared_channels import is_channel_shared
from daimon.core.turn_hold import TurnHold
from sqlalchemy.ext.asyncio import AsyncSession


async def routine_hold(session: AsyncSession, row: RoutineRow, *, platform: str) -> TurnHold | None:
    """The hold this routine's fire runs under, None when its destination is not shared."""
    channel_id = routine_destination_channel(row)
    if channel_id is None:
        return None
    if await is_channel_shared(
        session, tenant_id=row.tenant_id, platform=platform, channel_id=channel_id
    ):
        return TurnHold(platform, channel_id, client=False)
    return None
