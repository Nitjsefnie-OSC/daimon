"""Bill headless agent-chat turns once they finish, through the turn-end catch-up.

An MCP `start_turn`/`continue_turn` sends a `user.message` and returns: no
worker drives the session's stream, so no live recorder meters its model
calls. Before this module those turns were billed only by the usage sweep,
which is off by default, and their turn outcome was written at dispatch with
reason `unknown` and no usage.

`watch_turn` closes that hole from the dispatching process. It spawns one
bounded background task per dispatched turn, which never blocks the caller:

1. **Wait.** Poll the session's status (backing off from 2 s to 30 s) until
   it is `terminated`, or `idle` with a `session.status_idle` listed after the
   turn's own `user.message` (a bare `idle` right after a send may be the
   previous turn's state). The wait gives up after the turn ceiling.
2. **Bill.** Run `usage_reconcile.reconcile_unbilled` on the session with a
   recorder bound exactly as the chat path binds one (`record_turn_usage` at
   the session's frozen model, the deployment markup, the session's stamped
   budget channel, the `turn:<session>:<event>` key), so a call the sweep or
   another pass also records is debited once. Same 5-second budget, same
   durable progress; a pass that cannot finish leaves the rest to the next.
3. **Finish the outcome.** When the dispatch handed over its turn outcome,
   it is written now: the termination reason the session reached and this
   turn's model calls, tokens and cost (the calls listed after its boundary).

`catch_up_session` is the poll-side fallback: a status read that sees the
session idle or terminated runs one catch-up pass in the background, at most
once per 30 s per session and never while a watcher for the session is
pending. It covers turns whose watcher was lost to a restart.

A session stamped `daimon_billing_exempt` is never debited, as in the sweep;
its outcome still records the calls, with an exempt posture.
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic, omit
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.pricing import MODEL_PRICING
from daimon.core.turn.ceiling import TURN_CEILING_S
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.posture import UsageRecorder
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_reconcile import ReconcileResult, reconcile_unbilled
from daimon.core.usage_recording import record_turn_usage
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = [
    "DispatchedTurn",
    "catch_up_session",
    "drain",
    "headless_recorder",
    "settle_turn",
    "shutdown",
    "watch_turn",
]

POLL_INITIAL_S = 2.0
POLL_MAX_S = 30.0
POLL_BACKOFF = 1.5
CATCH_UP_INTERVAL_S = 30.0
MAX_WATCHERS = 256
# Status reads that fail in a row before the watcher gives up on the turn.
MAX_READ_FAILURES = 5
# Pages of the turn's listing read to find its end and its calls.
MAX_TURN_PAGES = 20

_TURN_EVENT_TYPES = [
    "user.message",
    "span.model_request_end",
    "session.status_idle",
    "session.status_terminated",
]

_watchers: dict[str, set[asyncio.Task[None]]] = {}
_catch_ups: set[asyncio.Task[None]] = set()
_last_catch_up: OrderedDict[str, float] = OrderedDict()
_MAX_THROTTLE_ENTRIES = 4096
# One pass at a time per session; an entry lives while a pass holds or awaits it.
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


@dataclass(frozen=True)
class DispatchedTurn:
    """A turn a headless caller sent: the session and the boundary its send returned."""

    session: BetaManagedAgentsSession
    tenant_id: uuid.UUID
    platform_user_id: str | None
    turn_event_id: str


def headless_recorder(
    sessionmaker: async_sessionmaker[AsyncSession],
    session: BetaManagedAgentsSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    markup: Decimal,
) -> UsageRecorder | None:
    """The recorder a chat turn on `session` would bind, or None if it is not billed.

    Mirrors `turn.prepare.bind_recorder`: `record_turn_usage` at the model
    frozen in the session's own agent snapshot, the deployment markup, and
    the budget channel stamped on the session at creation. None for a
    session stamped billing-exempt, or one whose tenant tag names another
    tenant (never debit a tenant for a session it does not own).
    """
    metadata = session.metadata or {}
    if metadata.get(MA_METADATA_KEY_BILLING_EXEMPT) is not None:
        return None
    tagged = metadata.get(MA_METADATA_KEY_TENANT)
    if tagged is not None and tagged != str(tenant_id):
        log.warning("billing.headless_tenant_mismatch", managed_session_id=session.id)
        return None
    model_id = session.agent.model.id
    pricing = MODEL_PRICING.get(model_id)
    if pricing is None:
        log.warning("billing.unpriced_model", model_id=model_id, ma_session_id=session.id)
    return functools.partial(
        record_turn_usage,
        sessionmaker=sessionmaker,
        platform_user_id=platform_user_id,
        managed_session_id=session.id,
        model_id=model_id,
        tenant_id=tenant_id,
        markup=markup,
        pricing=pricing,
        channel_id=metadata.get(MA_METADATA_KEY_BUDGET_CHANNEL),
    )


async def _reconcile(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    session_id: str,
    record: UsageRecorder,
) -> ReconcileResult | None:
    """One catch-up pass, one at a time per session in this process.

    Named boundary, as in the chat path: a failed pass is logged and the
    next one (or the sweep) retries whatever it could not write.
    """
    lock = _locks.get(session_id)
    if lock is None:
        lock = _locks[session_id] = asyncio.Lock()
    try:
        async with lock:
            return await reconcile_unbilled(
                anthropic=anthropic,
                sessionmaker=sessionmaker,
                managed_session_id=session_id,
                record=record,
            )
    except _anthropic.NotFoundError:
        log.info("billing.reconcile_skipped", managed_session_id=session_id)
    except (_anthropic.APIError, SQLAlchemyError):
        log.exception("billing.reconcile_failed", managed_session_id=session_id)
    return None


@dataclass
class _TurnRead:
    ended: bool
    reason: TerminationReason
    calls: list[BetaManagedAgentsSpanModelRequestEndEvent]
    complete: bool


def _idle_reason(event: object) -> TerminationReason:
    stop = getattr(getattr(event, "stop_reason", None), "type", None)
    if stop == "requires_action":
        return TerminationReason.REQUIRES_ACTION
    if stop == "retries_exhausted":
        return TerminationReason.UPSTREAM
    return TerminationReason.COMPLETED


async def _read_turn(anthropic: AsyncAnthropic, session_id: str, turn_event_id: str) -> _TurnRead:
    """Walk the session newest first down to the turn's own `user.message`.

    Newest first rather than filtered by the dispatcher's clock, so clock skew
    between this host and Managed Agents cannot hide the boundary. The turn
    has ended when a `session.status_idle` (or terminated) event is newer than
    the boundary; the `span.model_request_end` events newer than it are the
    turn's model calls.
    """
    calls: list[BetaManagedAgentsSpanModelRequestEndEvent] = []
    end: TerminationReason | None = None
    page: str | None = None
    for _ in range(MAX_TURN_PAGES):
        cursor = await anthropic.beta.sessions.events.list(
            session_id,
            order="desc",
            limit=100,
            types=_TURN_EVENT_TYPES,
            page=page if page is not None else omit,
        )
        for event in cursor.data:
            if event.id == turn_event_id:
                calls.reverse()
                return _TurnRead(
                    ended=end is not None,
                    reason=end or TerminationReason.UNKNOWN,
                    calls=calls,
                    complete=True,
                )
            if isinstance(event, BetaManagedAgentsSpanModelRequestEndEvent):
                calls.append(event)
            elif end is None and event.type == "session.status_idle":
                end = _idle_reason(event)
            elif end is None and event.type == "session.status_terminated":
                end = TerminationReason.SESSION_TERMINATED
        if cursor.next_page is None:
            break
        page = cursor.next_page
    # The boundary was not reached: not listed yet, or beyond the page bound.
    return _TurnRead(ended=False, reason=TerminationReason.UNKNOWN, calls=[], complete=False)


async def _wait_for_end(
    anthropic: AsyncAnthropic,
    session_id: str,
    turn_event_id: str,
    *,
    timeout_s: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> _TurnRead | None:
    """Poll until the turn has ended; None if the session is gone or the wait ran out."""
    deadline = clock() + timeout_s
    delay = POLL_INITIAL_S
    failures = 0
    while True:
        try:
            current = await anthropic.beta.sessions.retrieve(session_id)
            if current.status in ("idle", "terminated"):
                read = await _read_turn(anthropic, session_id, turn_event_id)
                if read.ended:
                    return read
                if current.status == "terminated":
                    return _TurnRead(
                        ended=True,
                        reason=TerminationReason.SESSION_TERMINATED,
                        calls=read.calls,
                        complete=read.complete,
                    )
            failures = 0
        except _anthropic.NotFoundError:
            log.info("billing.headless_session_gone", managed_session_id=session_id)
            return None
        except _anthropic.APIError:
            failures += 1
            if failures >= MAX_READ_FAILURES:
                log.warning("billing.headless_wait_failed", managed_session_id=session_id)
                return None
        remaining = deadline - clock()
        if remaining <= 0:
            log.warning("billing.headless_wait_expired", managed_session_id=session_id)
            return None
        await sleep(min(delay, remaining))
        delay = min(delay * POLL_BACKOFF, POLL_MAX_S)


def _finish_outcome(
    observation: TurnObservation,
    session: BetaManagedAgentsSession,
    read: _TurnRead | None,
    *,
    metered: bool,
) -> None:
    if read is None:
        observation.finish(reason=TerminationReason.UNKNOWN)
        return
    if read.complete:
        observation.session_id = session.id
        observation.model_by_session[session.id] = session.agent.model.id
        observation.usage_available = True
        for event in read.calls:
            observation.note_usage(event, metered=metered)
    observation.finish(reason=read.reason)


async def settle_turn(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    turn: DispatchedTurn,
    markup: Decimal,
    observation: TurnObservation | None = None,
    timeout_s: float = TURN_CEILING_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for `turn` to end, bill the session's unbilled calls, finish its outcome.

    Bills even when the wait gave up (the session gone excepted): the pass
    debits whatever is listed by then, and the next pass on the session
    resumes from its durable progress.
    """
    session = turn.session
    record = headless_recorder(
        sessionmaker,
        session,
        tenant_id=turn.tenant_id,
        platform_user_id=turn.platform_user_id,
        markup=markup,
    )
    read: _TurnRead | None = None
    try:
        read = await _wait_for_end(
            anthropic, session.id, turn.turn_event_id, timeout_s=timeout_s, sleep=sleep, clock=clock
        )
        if record is not None:
            await _reconcile(anthropic, sessionmaker, session.id, record)
    finally:
        if observation is not None:
            _finish_outcome(observation, session, read, metered=record is not None)


async def _guarded(name: str, session_id: str, work: Awaitable[None]) -> None:
    try:
        await work
    except asyncio.CancelledError:
        raise
    except Exception:
        # The task boundary: billing in the background must never take the
        # server down; the next pass on the session retries.
        log.exception(name, managed_session_id=session_id)


def _pending_watchers() -> int:
    return sum(len(tasks) for tasks in _watchers.values())


def watch_turn(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    turn: DispatchedTurn,
    markup: Decimal,
    observation: TurnObservation | None = None,
) -> None:
    """Spawn `settle_turn` for a dispatched turn; returns at once.

    At `MAX_WATCHERS` pending watchers the turn is not watched: its outcome
    is finished now as `unknown`, and the next catch-up on the session (a
    later turn, a status poll, or the sweep) bills it.
    """
    session_id = turn.session.id
    if _pending_watchers() >= MAX_WATCHERS:
        log.warning("billing.headless_watchers_full", managed_session_id=session_id)
        if observation is not None:
            observation.finish(reason=TerminationReason.UNKNOWN)
        return
    task = asyncio.create_task(
        _guarded(
            "billing.headless_settle_failed",
            session_id,
            settle_turn(
                anthropic=anthropic,
                sessionmaker=sessionmaker,
                turn=turn,
                markup=markup,
                observation=observation,
            ),
        ),
        name="billing.headless_settle",
    )
    tasks = _watchers.setdefault(session_id, set())
    tasks.add(task)

    def _done(t: asyncio.Task[None]) -> None:
        tasks.discard(t)
        if not tasks and _watchers.get(session_id) is tasks:
            del _watchers[session_id]

    task.add_done_callback(_done)


def catch_up_session(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    session: BetaManagedAgentsSession,
    tenant_id: uuid.UUID,
    platform_user_id: str | None,
    markup: Decimal,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Spawn one catch-up pass for a session seen idle or terminated; returns at once.

    Returns whether a pass was spawned: not while the session is running, has
    a pending watcher (which bills when the turn ends), was caught up in the
    last `CATCH_UP_INTERVAL_S`, or is not billed.
    """
    if session.status not in ("idle", "terminated") or session.id in _watchers:
        return False
    now = clock()
    last = _last_catch_up.get(session.id)
    if last is not None and now - last < CATCH_UP_INTERVAL_S:
        return False
    record = headless_recorder(
        sessionmaker,
        session,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        markup=markup,
    )
    if record is None:
        return False
    _last_catch_up[session.id] = now
    _last_catch_up.move_to_end(session.id)
    while len(_last_catch_up) > _MAX_THROTTLE_ENTRIES:
        _last_catch_up.popitem(last=False)

    async def run() -> None:
        await _reconcile(anthropic, sessionmaker, session.id, record)

    task = asyncio.create_task(
        _guarded("billing.headless_catch_up_failed", session.id, run()),
        name="billing.headless_catch_up",
    )
    _catch_ups.add(task)
    task.add_done_callback(_catch_ups.discard)
    return True


def _all_tasks() -> list[asyncio.Task[None]]:
    loop = asyncio.get_running_loop()
    tasks = [t for ts in _watchers.values() for t in ts] + list(_catch_ups)
    return [t for t in tasks if t.get_loop() is loop]


async def drain() -> None:
    """Await every pending watcher and catch-up (a test and shutdown barrier)."""
    tasks = _all_tasks()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def shutdown() -> None:
    """Cancel pending work on server shutdown.

    A cancelled watcher finishes its outcome as `unknown`; its turn's calls
    are billed by the next catch-up on the session.
    """
    tasks = _all_tasks()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
