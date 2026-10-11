"""Store for `usage_reconcile_progress`: the turn-end billing catch-up cursor.

Per `guideline:architecture`: this module does NOT swallow exceptions.
"""

from __future__ import annotations

from datetime import datetime

from daimon.core._models import UsageReconcileProgress
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_covered_through(session: AsyncSession, *, managed_session_id: str) -> datetime | None:
    """The newest metered call time recorded for the session, or None if never read."""
    stmt = select(UsageReconcileProgress.covered_through).where(
        UsageReconcileProgress.managed_session_id == managed_session_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def advance(session: AsyncSession, *, managed_session_id: str, through: datetime) -> None:
    """Move the cursor forward to `through`; never backwards (concurrent passes)."""
    stmt = pg_insert(UsageReconcileProgress).values(
        managed_session_id=managed_session_id, covered_through=through
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["managed_session_id"],
        set_={
            "covered_through": func.greatest(
                UsageReconcileProgress.covered_through, stmt.excluded.covered_through
            ),
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
