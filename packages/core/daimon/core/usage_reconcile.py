"""Debit model calls an MA session made while no worker was streaming it.

A turn's live recorder only meters the `span.model_request_end` events its
own SSE stream (or its reconnect replay of the current turn) delivers. When
the worker driving a turn dies -- a deploy restarting the adapter, a crash --
the MA session keeps running server-side, and every model call it makes from
then on reaches no recorder. The next turn on the same session replays only
its own suffix, so those calls stay unbilled for good unless the scheduler's
usage sweep is running (it is off by default: it lists the whole workspace).

`reconcile_unbilled` closes that hole from the turn path itself, once the
turn is over. It reads one session's `span.model_request_end` events in
`processed_at` order (the order MA lists them in, and the field its
`created_at[...]` filters compare), checks each page's ids against
`usage_events` (written in the same transaction as the ledger debit, so "has
a usage row" means "has a debit"), and pushes the missing ones through the
turn's own recorder. Pricing, markup, attribution and the
`turn:<session>:<event>` idempotency key are therefore exactly the live
path's; a call recorded concurrently by a live stream or the sweep is an
`ON CONFLICT DO NOTHING` no-op, never a second debit.

Progress is durable (`usage_reconcile_progress`) and exact:

- **Forward scan.** Resumes at the newest call time it handled, skipping the
  ids it already handled at exactly that time, so a resumed pass rereads at
  most the calls sharing that one timestamp. The page cap counts only pages
  that move the cursor, so a group of calls sharing a timestamp cannot
  starve it; every handled page is committed before the next is read.
- **Repair scan.** One page per pass of the history behind the forward
  cursor, on its own cursor that restarts from the top once it catches up.
  It catches a call MA made listable only after newer ones had been listed
  (MA does not document that listing is append-ordered), within one full
  rescan cycle of the session.

Bounds: the forward page cap, one repair page, and one wall-clock budget
around the whole pass, MA reads and database work alike.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import structlog
from anthropic import AsyncAnthropic, omit
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.stores import usage_events, usage_reconcile_progress
from daimon.core.turn.posture import UsageRecorder
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

RECONCILE_PAGE_SIZE = 100
RECONCILE_MAX_PAGES = 10
RECONCILE_BUDGET_S = 5.0

_TYPES = ["span.model_request_end"]


@dataclass
class _Mark:
    """An exact position in `processed_at` order: a time and the ids handled at it."""

    at: datetime | None = None
    ids: set[str] = field(default_factory=set[str])

    def covers(self, event: BetaManagedAgentsSpanModelRequestEndEvent) -> bool:
        if self.at is None:
            return False
        return event.processed_at < self.at or (
            event.processed_at == self.at and event.id in self.ids
        )

    def extend(self, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        if self.at is None or event.processed_at > self.at:
            self.at = event.processed_at
            self.ids = {event.id}
        elif event.processed_at == self.at:
            self.ids.add(event.id)


@dataclass
class _Tally:
    scanned: int = 0
    missing: int = 0
    repaired: int = 0
    debited: int = 0
    debit_usd: Decimal = Decimal("0")
    pages: int = 0


@dataclass(frozen=True)
class ReconcileResult:
    """What one pass did.

    `missing` counts calls with no usage row when their page was read (of
    which `repaired` were found by the repair scan); `debited`/`debit_usd`
    count only the ledger rows this pass actually inserted (a concurrent
    writer that won the key is not counted). `truncated` means the forward
    scan stopped at its page cap or the budget before reaching the end.
    """

    scanned: int
    missing: int
    repaired: int
    debited: int
    debit_usd: Decimal
    truncated: bool


async def _bill_page(
    calls: Sequence[BetaManagedAgentsSpanModelRequestEndEvent],
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    managed_session_id: str,
    record: UsageRecorder,
    tally: _Tally,
) -> int:
    """Debit the calls on one page with no usage row; return how many were missing."""
    tally.pages += 1
    tally.scanned += len(calls)
    async with sessionmaker() as s:
        recorded = await usage_events.recorded_among(
            s, managed_session_id=managed_session_id, event_ids=[e.id for e in calls]
        )
    missing = 0
    for event in calls:
        if event.id in recorded:
            continue
        missing += 1
        debit = await record(event=event)
        if debit is not None:
            tally.debited += 1
            tally.debit_usd += debit
    tally.missing += missing
    return missing


def _calls(data: Sequence[object]) -> list[BetaManagedAgentsSpanModelRequestEndEvent]:
    return [e for e in data if isinstance(e, BetaManagedAgentsSpanModelRequestEndEvent)]


async def reconcile_unbilled(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    managed_session_id: str,
    record: UsageRecorder,
    page_size: int = RECONCILE_PAGE_SIZE,
    max_pages: int = RECONCILE_MAX_PAGES,
    budget_s: float = RECONCILE_BUDGET_S,
) -> ReconcileResult:
    """Debit model calls on `managed_session_id` that have no ledger row yet.

    `record` must be the recorder bound to this session (`bind_recorder`), so
    the backfill prices at the session's frozen model and the deployment
    markup, exactly as the live stream would have. Errors from MA or the
    database propagate; the budget expiring does not (the pass is reported as
    truncated and its committed progress is kept).

    Logs `billing.reconciled_unbilled` on every pass that finds a missing call
    or is cut short.
    """
    started = time.monotonic()
    tally = _Tally()
    truncated = True
    try:
        async with asyncio.timeout(budget_s):
            async with sessionmaker() as s:
                progress = await usage_reconcile_progress.get(
                    s, managed_session_id=managed_session_id
                )
            forward = _Mark()
            if progress is not None:
                forward = _Mark(progress.covered_through, set(progress.covered_ids))
            forward_start = forward.at

            # Forward: from the exact position, oldest first.
            page = await anthropic.beta.sessions.events.list(
                managed_session_id,
                order="asc",
                limit=page_size,
                types=_TYPES,
                created_at_gte=forward.at if forward.at is not None else omit,
            )
            advancing = 0
            while True:
                calls = _calls(page.data)
                moved = any(not forward.covers(e) for e in calls)
                await _bill_page(
                    calls,
                    sessionmaker=sessionmaker,
                    managed_session_id=managed_session_id,
                    record=record,
                    tally=tally,
                )
                for event in calls:
                    forward.extend(event)
                if moved and forward.at is not None:
                    async with sessionmaker() as s, s.begin():
                        await usage_reconcile_progress.advance(
                            s,
                            managed_session_id=managed_session_id,
                            through=forward.at,
                            ids=forward.ids,
                        )
                    advancing += 1
                if not page.has_next_page():
                    truncated = False
                    break
                if advancing >= max_pages:
                    break
                page = await page.get_next_page()

            # Repair: one cursor-moving page of the history behind the forward
            # start, so a call listed late is reached within one rescan cycle.
            if forward_start is not None and progress is not None:
                repair = _Mark(progress.repair_through, set(progress.repair_ids))
                page = await anthropic.beta.sessions.events.list(
                    managed_session_id,
                    order="asc",
                    limit=page_size,
                    types=_TYPES,
                    created_at_gte=repair.at if repair.at is not None else omit,
                    created_at_lt=forward_start,
                )
                while True:
                    calls = _calls(page.data)
                    moved = any(not repair.covers(e) for e in calls)
                    tally.repaired += await _bill_page(
                        calls,
                        sessionmaker=sessionmaker,
                        managed_session_id=managed_session_id,
                        record=record,
                        tally=tally,
                    )
                    for event in calls:
                        repair.extend(event)
                    if not page.has_next_page() or moved:
                        break
                    page = await page.get_next_page()
                done = not page.has_next_page()
                async with sessionmaker() as s, s.begin():
                    await usage_reconcile_progress.set_repair(
                        s,
                        managed_session_id=managed_session_id,
                        through=None if done else repair.at,
                        ids=() if done else repair.ids,
                    )
    except TimeoutError:
        pass

    if tally.missing or truncated:
        log.info(
            "billing.reconciled_unbilled",
            managed_session_id=managed_session_id,
            scanned=tally.scanned,
            missing=tally.missing,
            repaired=tally.repaired,
            debited=tally.debited,
            debit_usd=str(tally.debit_usd),
            pages=tally.pages,
            truncated=truncated,
            elapsed_s=round(time.monotonic() - started, 3),
        )
    return ReconcileResult(
        scanned=tally.scanned,
        missing=tally.missing,
        repaired=tally.repaired,
        debited=tally.debited,
        debit_usd=tally.debit_usd,
        truncated=truncated,
    )
