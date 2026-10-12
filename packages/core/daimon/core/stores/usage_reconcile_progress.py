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

_BEGIN_PASS = text(
    """
    INSERT INTO usage_reconcile_progress (managed_session_id)
    VALUES (:sid)
    ON CONFLICT (managed_session_id) DO NOTHING
    """
)

_ADVANCE = text(
    """
    UPDATE usage_reconcile_progress SET
        covered_ids = CASE
            WHEN covered_through IS NULL OR CAST(:at AS timestamptz) > covered_through
                THEN CAST(:ids AS text[])
            WHEN CAST(:at AS timestamptz) = covered_through
                THEN ARRAY(SELECT DISTINCT unnest(covered_ids || CAST(:ids AS text[])))
            ELSE covered_ids
        END,
        covered_through = GREATEST(covered_through, CAST(:at AS timestamptz)),
        forward_since = :since,
        forward_page = :page,
        updated_at = now()
    WHERE managed_session_id = :sid
    """
)


@dataclass(frozen=True)
class Progress:
    """One session's catch-up state; see `UsageReconcileProgress`."""

    covered_through: datetime | None
    covered_ids: frozenset[str]
    forward_since: datetime | None
    forward_page: str | None
    repair_lt: datetime | None
    repair_since: datetime | None
    repair_page: str | None
    repair_starved: bool

    @property
    def repair_due(self) -> bool:
        """A repair cycle is open, or one may start: the forward scan has covered
        something and has no listing left unfinished (a backlog comes first)."""
        return self.repair_lt is not None or (
            self.covered_through is not None and self.forward_page is None
        )


async def begin_pass(session: AsyncSession, *, managed_session_id: str) -> Progress:
    """Return the state at the start of a pass (creating the row on first read).

    When a repair page is due, `repair_starved` is set before the pass reads
    anything; `set_repair` clears it once a repair page is handled. A pass
    that runs out of budget first so leaves it set, and the returned state
    still carries the previous pass's value, which orders this pass's scans.
    """
    await session.execute(_BEGIN_PASS, {"sid": managed_session_id})
    progress = await get(session, managed_session_id=managed_session_id)
    assert progress is not None  # just upserted in this transaction
    if progress.repair_due and not progress.repair_starved:
        await session.execute(
            update(UsageReconcileProgress)
            .where(UsageReconcileProgress.managed_session_id == managed_session_id)
            .values(repair_starved=True)
        )
    return progress


async def get(session: AsyncSession, *, managed_session_id: str) -> Progress | None:
    """The session's catch-up state, or None if the catch-up never read it."""
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
        forward_since=row.forward_since,
        forward_page=row.forward_page,
        repair_lt=row.repair_lt,
        repair_since=row.repair_since,
        repair_page=row.repair_page,
        repair_starved=row.repair_starved,
    )


async def advance(
    session: AsyncSession,
    *,
    managed_session_id: str,
    through: datetime | None,
    ids: Collection[str],
    since: datetime | None,
    page: str | None,
) -> None:
    """Record a handled forward page: the position and the listing's continuation.

    The position never moves backwards: a concurrent pass that reached a later
    time wins, and one that reached the same time adds its ids to the boundary
    set. `since`/`page` name the open listing (`page=None`: it reached its end).
    """
    if through is None:
        await session.execute(
            update(UsageReconcileProgress)
            .where(UsageReconcileProgress.managed_session_id == managed_session_id)
            .values(forward_since=since, forward_page=page, updated_at=func.now())
        )
        return
    await session.execute(
        _ADVANCE,
        {
            "sid": managed_session_id,
            "at": through,
            "ids": sorted(ids),
            "since": since,
            "page": page,
        },
    )


async def set_repair(
    session: AsyncSession,
    *,
    managed_session_id: str,
    lt: datetime | None,
    since: datetime | None,
    page: str | None,
) -> None:
    """Record a handled repair page's listing; `lt=None` ends the cycle."""
    await session.execute(
        update(UsageReconcileProgress)
        .where(UsageReconcileProgress.managed_session_id == managed_session_id)
        .values(
            repair_lt=lt,
            repair_since=since,
            repair_page=page,
            repair_starved=False,
            updated_at=func.now(),
        )
    )
