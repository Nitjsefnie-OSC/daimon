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

Progress is durable (`usage_reconcile_progress`):

- **Forward scan.** Continues the listing it left unfinished with MA's own
  page cursor, so a resumed pass reads the next unread page, never pages it
  already handled, even through calls sharing one timestamp. Once a listing
  reaches its end, the next one opens at the newest call time handled; the
  ids handled at exactly that time are kept for the boundary. Every handled
  page is committed, with its continuation, before the next is read.
- **Repair scan.** One page per pass of the history behind the forward
  position at the start of a cycle, continued the same way and restarted
  once it reaches that position. It catches a call MA made listable only
  after newer ones had been listed (MA does not document that listing is
  append-ordered).
- **Fairness.** The forward scan goes first. A repair cycle starts only once
  the forward scan has no unfinished listing (a backlog is billed first, and
  it shrinks every pass). When a pass runs out of budget before a due repair
  page, the next pass runs the repair scan first, so a slow forward read
  cannot starve it.

Bounds per pass: at most `max_pages` forward reads and one repair read (plus
one re-open each if MA rejects a stored page cursor), inside one wall-clock
budget around the whole pass, MA reads and database work alike.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from anthropic import AsyncAnthropic, BadRequestError, NotFoundError
from anthropic._models import construct_type
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.core.mux_compat import legacy_call
from daimon.core.stores import usage_events, usage_reconcile_progress
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.resources.session_tools import EventPage, EventQuery, SessionTools
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    # Annotation only: `daimon.core.turn` imports this module through `run`.
    from daimon.core.turn.posture import UsageRecorder

log = structlog.get_logger(__name__)

RECONCILE_PAGE_SIZE = 100
RECONCILE_MAX_PAGES = 10
RECONCILE_BUDGET_S = 5.0

_TYPES = ["span.model_request_end"]


@dataclass(frozen=True)
class _NativeEventPage:
    """Host SDK decoding edge; native paging remains behind SessionTools."""

    data: list[object]
    next_page: str | None
    native_page: EventPage

    def has_next_page(self) -> bool:
        # Match AsyncPageCursor, including empty pages and empty cursor strings.
        return bool(self.data) and bool(self.next_page)

    async def get_next_page(self) -> _NativeEventPage:
        if not self.has_next_page():
            raise RuntimeError("No next page expected; check has_next_page first")
        return _native_page(await legacy_call(self.native_page.continue_page()))


def _native_page(page: EventPage) -> _NativeEventPage:
    return _NativeEventPage(
        data=[
            construct_type(value=item.native, type_=BetaManagedAgentsSessionEvent)
            for item in page.data
        ],
        next_page=page.next_page,
        native_page=page,
    )


@dataclass(frozen=True)
class _NativeEventHistory:
    port: SessionTools
    scope: Scope
    session: ResourceRef

    async def read(self, query: EventQuery) -> _NativeEventPage:
        page = await legacy_call(self.port.list_events(self.scope, self.session, query))
        return _native_page(page)

    async def list(
        self, *, page_size: int, since: datetime | None, lt: datetime | None, token: str | None
    ) -> _NativeEventPage:
        query = EventQuery(order="asc", limit=page_size, types=_TYPES)
        # Unset fields mean SDK omit; supplied datetime values reach its native
        # serializer unchanged. Never turn omitted values into explicit nulls.
        if since is not None:
            query = query.model_copy(update={"created_at_gte": since})
        if lt is not None:
            query = query.model_copy(update={"created_at_lt": lt})
        if token is not None:
            query = query.model_copy(update={"page": token})
        return await self.read(query)


def _native_history(
    client: AsyncAnthropic, scope: Scope | None, session_id: str
) -> _NativeEventHistory:
    if (
        scope is None
        or scope.is_platform
        or scope.is_legacy_host_authorized
        or not scope.tenant_id.strip()
        or not scope.account_id.strip()
    ):
        raise ScopeViolation(
            session_id, "billing recovery requires an admitted tenant/account scope"
        )
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    return _NativeEventHistory(
        backend.extension(SessionTools, namespace="anthropic.session_tools", version=1),
        scope,
        resource_ref(backend, "session", session_id, scope=scope),
    )


@dataclass
class _Mark:
    """A position in `processed_at` order: a time and the ids handled at it."""

    at: datetime | None = None
    ids: set[str] = field(default_factory=set[str])

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
    forward_done: bool = False
    repair_due: bool = False
    repair_read: bool = False


@dataclass(frozen=True)
class ReconcileResult:
    """What one pass did.

    `missing` counts calls with no usage row when their page was read (of
    which `repaired` were found by the repair scan); `debited`/`debit_usd`
    count only the ledger rows this pass actually inserted (a concurrent
    writer that won the key is not counted). `truncated` means the forward
    scan did not reach the end of the listing; `repair_skipped` means a
    repair page was due but the budget ran out before it was read.
    """

    scanned: int
    missing: int
    repaired: int
    debited: int
    debit_usd: Decimal
    truncated: bool
    repair_skipped: bool


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
    if not calls:
        return 0
    async with sessionmaker() as s:
        recorded = await usage_events.recorded_among(
            s, managed_session_id=managed_session_id, event_ids=[e.id for e in calls]
        )
    missing = 0
    for event in calls:
        if event.id in recorded:
            continue
        missing += 1
        tally.missing += 1
        debit = await record(event=event)
        if debit is not None:
            tally.debited += 1
            tally.debit_usd += debit
    return missing


def _calls(data: Sequence[object]) -> list[BetaManagedAgentsSpanModelRequestEndEvent]:
    return [e for e in data if isinstance(e, BetaManagedAgentsSpanModelRequestEndEvent)]


def _next(page: _NativeEventPage) -> str | None:
    return page.next_page if page.has_next_page() else None


async def _open(
    history: _NativeEventHistory,
    managed_session_id: str,
    *,
    page_size: int,
    since: datetime | None,
    lt: datetime | None,
    token: str | None,
) -> _NativeEventPage:
    """Open (or, with `token`, continue) a listing; re-open it if MA rejects the cursor."""

    async def _list(page: str | None) -> _NativeEventPage:
        return await history.list(page_size=page_size, since=since, lt=lt, token=page)

    if token is None:
        return await _list(None)
    try:
        return await _list(token)
    except (BadRequestError, NotFoundError):
        # An expired or unknown cursor: start the same listing over. A session
        # that is really gone 404s again here and propagates.
        log.info("billing.reconcile_cursor_reset", managed_session_id=managed_session_id)
        return await _list(None)


async def _forward(
    history: _NativeEventHistory,
    sessionmaker: async_sessionmaker[AsyncSession],
    managed_session_id: str,
    record: UsageRecorder,
    progress: usage_reconcile_progress.Progress,
    *,
    page_size: int,
    max_pages: int,
    tally: _Tally,
) -> None:
    position = _Mark(progress.covered_through, set(progress.covered_ids))
    if progress.forward_page is not None:
        since, token = progress.forward_since, progress.forward_page
    else:
        since, token = position.at, None
    page = await _open(
        history, managed_session_id, page_size=page_size, since=since, lt=None, token=token
    )
    reads = 1
    stored = (
        progress.covered_through,
        progress.covered_ids,
        progress.forward_since,
        progress.forward_page,
    )
    while True:
        calls = _calls(page.data)
        await _bill_page(
            calls,
            sessionmaker=sessionmaker,
            managed_session_id=managed_session_id,
            record=record,
            tally=tally,
        )
        for event in calls:
            position.extend(event)
        following = _next(page)
        state = (position.at, frozenset(position.ids), since, following)
        if state != stored:  # a caught-up rescan of the boundary writes nothing
            async with sessionmaker() as s, s.begin():
                await usage_reconcile_progress.advance(
                    s,
                    managed_session_id=managed_session_id,
                    through=position.at,
                    ids=position.ids,
                    since=since,
                    page=following,
                )
            stored = state
        if following is None:
            tally.forward_done = True
            return
        if reads >= max_pages:
            return
        page = await page.get_next_page()
        reads += 1


async def _repair(
    history: _NativeEventHistory,
    sessionmaker: async_sessionmaker[AsyncSession],
    managed_session_id: str,
    record: UsageRecorder,
    progress: usage_reconcile_progress.Progress,
    *,
    page_size: int,
    tally: _Tally,
) -> None:
    if not progress.repair_due:
        return  # nothing covered yet, or a forward backlog comes first
    if progress.repair_lt is not None:
        lt, since, token = progress.repair_lt, progress.repair_since, progress.repair_page
    else:
        assert progress.covered_through is not None  # `repair_due`
        lt, since, token = progress.covered_through, None, None  # a new cycle
    page = await _open(
        history, managed_session_id, page_size=page_size, since=since, lt=lt, token=token
    )
    calls = _calls(page.data)
    tally.repaired += await _bill_page(
        calls,
        sessionmaker=sessionmaker,
        managed_session_id=managed_session_id,
        record=record,
        tally=tally,
    )
    tally.repair_read = True
    following = _next(page)
    async with sessionmaker() as s, s.begin():
        # The cursor is only valid with the filters it was issued for, so the
        # listing's own `since`/`lt` are kept until the cycle ends.
        await usage_reconcile_progress.set_repair(
            s,
            managed_session_id=managed_session_id,
            lt=lt if following is not None else None,
            since=since if following is not None else None,
            page=following,
        )


async def reconcile_unbilled(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    managed_session_id: str,
    record: UsageRecorder,
    scope: Scope,
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

    Logs `billing.reconciled_unbilled` on every pass that finds a missing
    call, leaves the forward listing unfinished, or misses a due repair page.
    """
    history = _native_history(anthropic, scope, managed_session_id)
    started = time.monotonic()
    tally = _Tally()
    try:
        async with asyncio.timeout(budget_s):
            async with sessionmaker() as s, s.begin():
                progress = await usage_reconcile_progress.begin_pass(
                    s, managed_session_id=managed_session_id
                )
            tally.repair_due = progress.repair_due

            async def forward() -> None:
                await _forward(
                    history,
                    sessionmaker,
                    managed_session_id,
                    record,
                    progress,
                    page_size=page_size,
                    max_pages=max_pages,
                    tally=tally,
                )

            async def repair() -> None:
                await _repair(
                    history,
                    sessionmaker,
                    managed_session_id,
                    record,
                    progress,
                    page_size=page_size,
                    tally=tally,
                )

            # Forward first, unless the last pass ran out of budget before its
            # due repair page: then repair goes first this time.
            if progress.repair_starved and progress.repair_due:
                await repair()
                await forward()
            else:
                await forward()
                await repair()
    except TimeoutError:
        pass  # committed progress is kept; the flags say what did not finish

    truncated = not tally.forward_done
    repair_skipped = tally.repair_due and not tally.repair_read
    if tally.missing or truncated or repair_skipped:
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
            repair_skipped=repair_skipped,
            elapsed_s=round(time.monotonic() - started, 3),
        )
    return ReconcileResult(
        scanned=tally.scanned,
        missing=tally.missing,
        repaired=tally.repaired,
        debited=tally.debited,
        debit_usd=tally.debit_usd,
        truncated=truncated,
        repair_skipped=repair_skipped,
    )
