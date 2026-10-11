"""Debit model calls an MA session made while no worker was streaming it.

A turn's live recorder only meters the `span.model_request_end` events its
own SSE stream (or its reconnect replay of the current turn) delivers. When
the worker driving a turn dies -- a deploy restarting the adapter, a crash --
the MA session keeps running server-side, and every model call it makes from
then on reaches no recorder. The next turn on the same session replays only
its own suffix, so those calls stay unbilled for good unless the scheduler's
usage sweep is running (it is off by default: it lists the whole workspace).

`reconcile_unbilled` closes that hole from the turn path itself. It lists one
session's `span.model_request_end` events, skips the ones `usage_events`
already holds (the same transaction as the ledger debit, so "has a usage row"
means "has a debit"), and pushes the rest through the turn's own recorder.
Pricing, markup, attribution and the `turn:<session>:<event>` idempotency key
are therefore exactly the live path's; a call recorded concurrently by a live
stream or the sweep is an `ON CONFLICT DO NOTHING` no-op, never a second debit.

The read is bounded by a page cap and a wall-clock budget. Events are read
newest first, so a cut-short pass still covers the most recent gap; whatever
it misses is picked up by the next reconcile on the same session (or the
sweep). Each debit is its own transaction, so partial progress is kept.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.turn.posture import UsageRecorder
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

ReconcileTrigger = Literal["attach", "turn_end"]

RECONCILE_PAGE_SIZE = 100
RECONCILE_MAX_PAGES = 20
RECONCILE_BUDGET_S = 15.0


@dataclass(frozen=True)
class ReconcileResult:
    """What one pass found. `debited` counts calls handed to the recorder."""

    scanned: int
    debited: int
    debit_usd: Decimal
    truncated: bool


async def reconcile_unbilled(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    managed_session_id: str,
    record: UsageRecorder,
    trigger: ReconcileTrigger,
    page_size: int = RECONCILE_PAGE_SIZE,
    max_pages: int = RECONCILE_MAX_PAGES,
    budget_s: float = RECONCILE_BUDGET_S,
) -> ReconcileResult:
    """Debit every model call on `managed_session_id` that has no ledger row yet.

    `record` must be the recorder bound to this session (`bind_recorder`), so
    the backfill prices at the session's frozen model and the deployment
    markup, exactly as the live stream would have. Exceptions from MA or the
    database propagate; the caller decides whether a failed backfill may end
    the turn.

    Logs `billing.reconciled_unbilled` on every pass that debits something or
    is cut short, with the counts and the USD actually written to the ledger.
    """
    started = time.monotonic()
    async with sessionmaker() as s:
        recorded = await usage_events.list_event_ids_for_session(
            s, managed_session_id=managed_session_id
        )

    scanned = 0
    debited_keys: list[str] = []
    truncated = False
    pages = 0
    try:
        async with asyncio.timeout(budget_s):
            page = await anthropic.beta.sessions.events.list(
                managed_session_id,
                order="desc",
                limit=page_size,
                types=["span.model_request_end"],
            )
            while True:
                pages += 1
                for event in page.data:
                    if not isinstance(event, BetaManagedAgentsSpanModelRequestEndEvent):
                        continue
                    scanned += 1
                    if event.id in recorded:
                        continue
                    await record(event=event)
                    debited_keys.append(f"turn:{managed_session_id}:{event.id}")
                if not page.has_next_page():
                    break
                if pages >= max_pages:
                    truncated = True
                    break
                page = await page.get_next_page()
    except TimeoutError:
        truncated = True

    debit_usd = Decimal("0")
    if debited_keys:
        async with sessionmaker() as s:
            debit_usd = -await tenant_ledger.sum_for_idempotency_keys(s, keys=debited_keys)
    if debited_keys or truncated:
        log.info(
            "billing.reconciled_unbilled",
            managed_session_id=managed_session_id,
            trigger=trigger,
            scanned=scanned,
            debited=len(debited_keys),
            debit_usd=str(debit_usd),
            pages=pages,
            truncated=truncated,
            elapsed_s=round(time.monotonic() - started, 3),
        )
    return ReconcileResult(
        scanned=scanned, debited=len(debited_keys), debit_usd=debit_usd, truncated=truncated
    )
