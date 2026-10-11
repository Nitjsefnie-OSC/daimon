"""Debit model calls an MA session made while no worker was streaming it.

A turn's live recorder only meters the `span.model_request_end` events its
own SSE stream (or its reconnect replay of the current turn) delivers. When
the worker driving a turn dies -- a deploy restarting the adapter, a crash --
the MA session keeps running server-side, and every model call it makes from
then on reaches no recorder. The next turn on the same session replays only
its own suffix, so those calls stay unbilled for good unless the scheduler's
usage sweep is running (it is off by default: it lists the whole workspace).

`reconcile_unbilled` closes that hole from the turn path itself, once the
turn is over. It reads one session's `span.model_request_end` events oldest
first, from a durable per-session cursor (`usage_reconcile_progress`), checks
each page's ids against `usage_events` (the same transaction as the ledger
debit, so "has a usage row" means "has a debit"), and pushes the rest through
the turn's own recorder. Pricing, markup, attribution and the
`turn:<session>:<event>` idempotency key are therefore exactly the live
path's; a call recorded concurrently by a live stream or the sweep is an
`ON CONFLICT DO NOTHING` no-op, never a second debit.

Bounds: a page cap per pass and one wall-clock budget around the whole
operation, MA reads and database work alike. The cursor advances after every
fully handled page, so a pass that is cut short resumes where it stopped and
a long backlog is worked through over successive turns rather than reread.
The cursor resumes `_OVERLAP` before the newest call it covered, so a call
that becomes listable slightly late is still seen; ids already metered in
the overlap cost one indexed lookup, not a debit.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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
_OVERLAP = timedelta(minutes=5)


@dataclass(frozen=True)
class ReconcileResult:
    """What one pass did.

    `missing` counts calls with no usage row when their page was read;
    `debited`/`debit_usd` count only the ledger rows this pass actually
    inserted (a concurrent writer that won the key is not counted).
    """

    scanned: int
    missing: int
    debited: int
    debit_usd: Decimal
    truncated: bool


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
    scanned = 0
    missing = 0
    debited = 0
    debit_usd = Decimal("0")
    pages = 0
    truncated = False
    try:
        async with asyncio.timeout(budget_s):
            async with sessionmaker() as s:
                covered = await usage_reconcile_progress.get_covered_through(
                    s, managed_session_id=managed_session_id
                )
            page = await anthropic.beta.sessions.events.list(
                managed_session_id,
                order="asc",
                limit=page_size,
                types=["span.model_request_end"],
                created_at_gte=covered - _OVERLAP if covered is not None else omit,
            )
            while True:
                pages += 1
                calls = [
                    e for e in page.data if isinstance(e, BetaManagedAgentsSpanModelRequestEndEvent)
                ]
                scanned += len(calls)
                async with sessionmaker() as s:
                    recorded = await usage_events.recorded_among(
                        s,
                        managed_session_id=managed_session_id,
                        event_ids=[e.id for e in calls],
                    )
                through: datetime | None = None
                for event in calls:
                    if event.id not in recorded:
                        missing += 1
                        debit = await record(event=event)
                        if debit is not None:
                            debited += 1
                            debit_usd += debit
                    if through is None or event.processed_at > through:
                        through = event.processed_at
                if through is not None:
                    async with sessionmaker() as s, s.begin():
                        await usage_reconcile_progress.advance(
                            s, managed_session_id=managed_session_id, through=through
                        )
                if not page.has_next_page():
                    break
                if pages >= max_pages:
                    truncated = True
                    break
                page = await page.get_next_page()
    except TimeoutError:
        truncated = True

    if missing or truncated:
        log.info(
            "billing.reconciled_unbilled",
            managed_session_id=managed_session_id,
            scanned=scanned,
            missing=missing,
            debited=debited,
            debit_usd=str(debit_usd),
            pages=pages,
            truncated=truncated,
            elapsed_s=round(time.monotonic() - started, 3),
        )
    return ReconcileResult(
        scanned=scanned,
        missing=missing,
        debited=debited,
        debit_usd=debit_usd,
        truncated=truncated,
    )
