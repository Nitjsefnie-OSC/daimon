"""Store for per-session settle state: the billing watermark and pending settle requests.

Per `guideline:architecture`: this module does NOT swallow exceptions.
"""

from __future__ import annotations

from datetime import datetime

from daimon.core._models import SessionSettleState
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

BACKLOG_CUTOFF_ID = "backlog-cutoff"
"""The row whose `covered_through` is when this settle path was deployed.

Written once by the migration that introduced settle requests. Calls Managed
Agents made before it are the backlog: no automatic settle reads them; only
the operator's backlog settle does.
"""


async def covered_through(session: AsyncSession, *, managed_session_id: str) -> datetime | None:
    """The newest call time a completed settle of the session read, if any."""
    return (
        await session.execute(
            select(SessionSettleState.covered_through).where(
                SessionSettleState.managed_session_id == managed_session_id
            )
        )
    ).scalar_one_or_none()


async def backlog_cutoff(session: AsyncSession) -> datetime | None:
    """When this settle path was deployed (None on a database that never ran it)."""
    return await covered_through(session, managed_session_id=BACKLOG_CUTOFF_ID)


async def advance(session: AsyncSession, *, managed_session_id: str, through: datetime) -> None:
    """Move the watermark to `through`; it never moves backwards."""
    stmt = insert(SessionSettleState).values(
        managed_session_id=managed_session_id, covered_through=through
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SessionSettleState.managed_session_id],
            set_={
                "covered_through": func.greatest(
                    SessionSettleState.covered_through, stmt.excluded.covered_through
                ),
                "updated_at": func.now(),
            },
        )
    )


async def request_settle(
    session: AsyncSession, *, managed_session_id: str, turn_event_id: str
) -> None:
    """Record that the turn sent as `turn_event_id` must be settled once it ends.

    A later send on the session replaces the request: settling it covers the
    earlier turn's calls too.
    """
    stmt = insert(SessionSettleState).values(
        managed_session_id=managed_session_id,
        settle_turn_event_id=turn_event_id,
        settle_requested_at=func.now(),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SessionSettleState.managed_session_id],
            set_={
                "settle_turn_event_id": stmt.excluded.settle_turn_event_id,
                "settle_requested_at": stmt.excluded.settle_requested_at,
                "updated_at": func.now(),
            },
        )
    )


async def settle_request(session: AsyncSession, *, managed_session_id: str) -> str | None:
    """The turn a pending settle request waits on, or None if none is pending."""
    return (
        await session.execute(
            select(SessionSettleState.settle_turn_event_id).where(
                SessionSettleState.managed_session_id == managed_session_id,
                SessionSettleState.settle_requested_at.is_not(None),
            )
        )
    ).scalar_one_or_none()


async def clear_settle_request(
    session: AsyncSession, *, managed_session_id: str, turn_event_id: str
) -> None:
    """Clear the request for `turn_event_id`; a newer send's request is kept."""
    await session.execute(
        update(SessionSettleState)
        .where(
            SessionSettleState.managed_session_id == managed_session_id,
            SessionSettleState.settle_turn_event_id == turn_event_id,
        )
        .values(settle_turn_event_id=None, settle_requested_at=None, updated_at=func.now())
    )


async def list_settle_requests(session: AsyncSession, *, limit: int) -> list[tuple[str, str]]:
    """Up to `limit` pending requests, oldest first, as (session id, turn event id)."""
    rows = await session.execute(
        select(SessionSettleState.managed_session_id, SessionSettleState.settle_turn_event_id)
        .where(SessionSettleState.settle_requested_at.is_not(None))
        .order_by(SessionSettleState.settle_requested_at)
        .limit(limit)
    )
    return [(sid, turn) for sid, turn in rows.tuples() if turn is not None]
