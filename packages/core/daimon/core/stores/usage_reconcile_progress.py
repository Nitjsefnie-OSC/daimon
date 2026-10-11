"""Store for `usage_reconcile_progress`: the turn-end billing catch-up cursors.

Per `guideline:architecture`: this module does NOT swallow exceptions.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from daimon.core._models import UsageReconcileProgress
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

_ADVANCE = text(
    """
    INSERT INTO usage_reconcile_progress (managed_session_id, covered_through, covered_ids)
    VALUES (:sid, :at, CAST(:ids AS text[]))
    ON CONFLICT (managed_session_id) DO UPDATE SET
        covered_through = GREATEST(
            usage_reconcile_progress.covered_through, EXCLUDED.covered_through
        ),
        covered_ids = CASE
            WHEN EXCLUDED.covered_through > usage_reconcile_progress.covered_through
                THEN EXCLUDED.covered_ids
            WHEN EXCLUDED.covered_through = usage_reconcile_progress.covered_through
                THEN ARRAY(
                    SELECT DISTINCT unnest(
                        usage_reconcile_progress.covered_ids || EXCLUDED.covered_ids
                    )
                )
            ELSE usage_reconcile_progress.covered_ids
        END,
        updated_at = now()
    """
)


@dataclass(frozen=True)
class Progress:
    """Both continuations for one session; see `UsageReconcileProgress`."""

    covered_through: datetime
    covered_ids: frozenset[str]
    repair_through: datetime | None
    repair_ids: frozenset[str]


async def get(session: AsyncSession, *, managed_session_id: str) -> Progress | None:
    """The session's cursors, or None if the catch-up never read it."""
    row = (
        await session.execute(
            select(UsageReconcileProgress).where(
                UsageReconcileProgress.managed_session_id == managed_session_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return Progress(
        covered_through=row.covered_through,
        covered_ids=frozenset(row.covered_ids),
        repair_through=row.repair_through,
        repair_ids=frozenset(row.repair_ids),
    )


async def advance(
    session: AsyncSession,
    *,
    managed_session_id: str,
    through: datetime,
    ids: Collection[str],
) -> None:
    """Move the forward cursor to (`through`, `ids`); never backwards.

    A concurrent pass that reached a later time wins; one that reached the
    same time contributes its ids to the boundary set.
    """
    await session.execute(
        _ADVANCE,
        {"sid": managed_session_id, "at": through, "ids": sorted(ids)},
    )


async def set_repair(
    session: AsyncSession,
    *,
    managed_session_id: str,
    through: datetime | None,
    ids: Collection[str],
) -> None:
    """Record where the repair rescan stopped; `through=None` restarts it from the top."""
    await session.execute(
        update(UsageReconcileProgress)
        .where(UsageReconcileProgress.managed_session_id == managed_session_id)
        .values(repair_through=through, repair_ids=sorted(ids), updated_at=func.now())
    )
