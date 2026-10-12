"""Bill headless agent-chat turns once they finish, through the turn-end catch-up.

An MCP `start_turn`/`continue_turn`/`ask` sends a `user.message` and returns:
no worker drives the session's stream, so no live recorder meters its model
calls. Before this module those turns were billed only by the usage sweep,
which is off by default, and their turn outcome was written at dispatch with
reason `unknown` and no usage.

`watch_turn` closes that hole from the dispatching process. Every session
with a dispatched turn gets one background watcher (turns on the same session
share it), which never blocks the caller:

1. **Wait.** Poll the session's status (backing off from 2 s to 30 s) until
   every pending turn has ended: a `session.status_idle` or
   `session.status_terminated` event listed after the turn's own
   `user.message` and before the next one (a bare `idle` right after a send
   may be the previous turn's state). Each upstream read runs under a
   20-second timeout that also stops at the watcher's deadline (each turn's
   own dispatch plus the turn ceiling), on a background client with no SDK
   retries. A watcher holds at most 16 turns; past that the oldest is
   written `unknown`.
2. **Finish the outcomes.** A turn handed over by its dispatch is written
   with the reason it reached and its own model calls (those between its
   message and its first terminal event). A turn that cannot be resolved
   (deadline, repeated read failures, a boundary not found in the bounded
   transcript walk three times) is written as `unknown` with no usage.
3. **Bill.** Run `usage_reconcile.reconcile_unbilled` on the session with a
   recorder bound exactly as the chat path binds one (`record_turn_usage` at
   the session's frozen model, the deployment markup, the session's stamped
   budget channel, the `turn:<session>:<event>` key), so a call the sweep or
   another pass also records is debited once. A pass that fails, or leaves
   the listing unfinished, is retried (a truncated pass at once, a failed one
   with backoff from 5 s to 5 min), up to 10 passes. A session still not
   settled after that is marked unsettled for the poll fallback.

**Rate limits.** Any 429 from a background read or pass starts one cooldown
shared by the whole process (one Managed Agents workspace), from its
`retry-after` (30 s without one, at most 5 min). No watcher reads, settlement
pass or fallback pass starts before it ends; a newly dispatched turn does not
cut it short, and a status poll during it queues nothing.

`catch_up_session` is the poll-side fallback: a status read that sees the
session idle or terminated runs up to three catch-up passes in the
background (a failed pass retried after 5 s, doubling), unless a watcher for
the session is pending (it settles the session itself), a pass for it is
already queued, a 429 cooldown is running, or it was caught up in the last
30 s (5 s for a session whose last settlement did not finish).

Bounds per process: 256 watched sessions, 32 queued fallback catch-ups, 16
concurrent background status/transcript reads, 4 concurrent catch-up passes.

A session stamped `daimon_billing_exempt` is never debited, as in the sweep;
its outcomes still record the calls, with an exempt posture.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import math
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic, omit
from anthropic.pagination import AsyncPageCursor
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
)
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
# One upstream read, at most (and never past the watcher's deadline).
REQUEST_TIMEOUT_S = 20.0
# No SDK retries for background reads: the interactive client's eight (and its
# sleeps on `retry-after`) would let one logical read outlast the watcher's own
# pacing, which retries failed reads and passes itself.
BACKGROUND_MAX_RETRIES = 0
RATE_LIMIT_MAX_PAUSE_S = 300.0
# Status reads that fail in a row before the watcher stops waiting.
MAX_READ_FAILURES = 5
# Pages (of 100) one transcript walk reads looking for the pending boundaries.
MAX_TURN_PAGES = 10
# Idle walks that miss a turn's boundary before it is written as `unknown`.
MAX_UNRESOLVED_WALKS = 3
SETTLE_MAX_PASSES = 10
SETTLE_RETRY_INITIAL_S = 5.0
SETTLE_RETRY_MAX_S = 300.0
# Pause before continuing a pass that ran out of budget mid-listing.
SETTLE_CONTINUE_S = 1.0
CATCH_UP_INTERVAL_S = 30.0
# The shorter gap for a session whose last settlement did not finish.
CATCH_UP_RETRY_S = 5.0
CATCH_UP_MAX_PASSES = 3
MAX_WATCHERS = 256
# Turns one session's watcher holds; past it the oldest is written `unknown`.
MAX_PENDING_PER_SESSION = 16
MAX_CATCH_UPS = 32
MAX_CONCURRENT_READS = 16
MAX_CONCURRENT_PASSES = 4

_TURN_EVENT_TYPES = [
    "user.message",
    "span.model_request_end",
    "session.status_idle",
    "session.status_terminated",
]

_MAX_REMEMBERED = 4096


@dataclass(frozen=True)
class DispatchedTurn:
    """A turn a headless caller sent: the session and the boundary its send returned."""

    session: BetaManagedAgentsSession
    tenant_id: uuid.UUID
    platform_user_id: str | None
    turn_event_id: str


@dataclass
class _PendingTurn:
    turn_event_id: str
    observation: TurnObservation | None
    # This turn's own wait deadline: its dispatch plus the turn ceiling.
    deadline: float
    unresolved_walks: int = 0


@dataclass
class _Watch:
    """One session's watcher state: the turns still waiting to end, and how to bill."""

    session: BetaManagedAgentsSession
    record: UsageRecorder | None
    deadline: float
    pending: dict[str, _PendingTurn] = field(default_factory=dict[str, _PendingTurn])
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def finalize(self, reason: TerminationReason = TerminationReason.UNKNOWN) -> None:
        """Finish every pending outcome (`finish` is a no-op on one already written)."""
        for turn in self.pending.values():
            if turn.observation is not None:
                turn.observation.finish(reason=reason)
        self.pending.clear()

    def add(self, turn: _PendingTurn) -> None:
        """Hold one more turn, writing the oldest as `unknown` past the per-session cap."""
        self.pending[turn.turn_event_id] = turn
        while len(self.pending) > MAX_PENDING_PER_SESSION:
            oldest = self.pending.pop(next(iter(self.pending)))
            log.warning(
                "billing.headless_pending_full",
                managed_session_id=self.session.id,
                turn_event_id=oldest.turn_event_id,
            )
            if oldest.observation is not None:
                oldest.observation.finish(reason=TerminationReason.UNKNOWN)
        self.deadline = max(t.deadline for t in self.pending.values())

    def expire(self, now: float) -> None:
        """Write every turn past its own deadline as `unknown`."""
        for turn in [t for t in self.pending.values() if t.deadline <= now]:
            log.warning(
                "billing.headless_wait_expired",
                managed_session_id=self.session.id,
                turn_event_id=turn.turn_event_id,
            )
            del self.pending[turn.turn_event_id]
            if turn.observation is not None:
                turn.observation.finish(reason=TerminationReason.UNKNOWN)


_watches: dict[str, _Watch] = {}
# Shared by every background read and pass in this process (one MA workspace):
# no background request is sent before this `time.monotonic()` reading.
_cooldown_until = 0.0
_watch_tasks: dict[str, asyncio.Task[None]] = {}
_catch_ups: dict[str, asyncio.Task[None]] = {}
_last_catch_up: OrderedDict[str, float] = OrderedDict()
_unsettled: OrderedDict[str, None] = OrderedDict()
# One pass at a time per session; an entry lives while a pass holds or awaits it.
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_limits: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[asyncio.Semaphore, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _limit(kind: Literal["read", "pass"]) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limits = _limits.get(loop)
    if limits is None:
        limits = _limits[loop] = (
            asyncio.Semaphore(MAX_CONCURRENT_READS),
            asyncio.Semaphore(MAX_CONCURRENT_PASSES),
        )
    return limits[0] if kind == "read" else limits[1]


def _remember(mapping: OrderedDict[str, None] | OrderedDict[str, float], key: str) -> None:
    mapping.move_to_end(key)
    while len(mapping) > _MAX_REMEMBERED:
        mapping.popitem(last=False)


def _mark_unsettled(session_id: str) -> None:
    _unsettled[session_id] = None
    _remember(_unsettled, session_id)


def _background(anthropic: AsyncAnthropic) -> AsyncAnthropic:
    """The client for background reads: a per-request timeout and no SDK retries."""
    return anthropic.with_options(max_retries=BACKGROUND_MAX_RETRIES, timeout=REQUEST_TIMEOUT_S)


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


# ---------------------------------------------------------------------------
# Billing passes
# ---------------------------------------------------------------------------


async def _reconcile(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    session_id: str,
    record: UsageRecorder,
) -> ReconcileResult | None:
    """One catch-up pass, one at a time per session in this process.

    Returns None when the pass failed: logged, and retried by the caller.
    Reads go through the background client, so retries are the caller's.
    """
    lock = _locks.get(session_id)
    if lock is None:
        lock = _locks[session_id] = asyncio.Lock()
    try:
        async with _limit("pass"), lock:
            return await reconcile_unbilled(
                anthropic=_background(anthropic),
                sessionmaker=sessionmaker,
                managed_session_id=session_id,
                record=record,
            )
    except _anthropic.RateLimitError as err:
        _note_rate_limit(err, session_id=session_id)
        return None
    except _anthropic.NotFoundError:
        # A deleted session: nothing left to list, so nothing left to bill.
        log.info("billing.reconcile_skipped", managed_session_id=session_id)
        return ReconcileResult(
            scanned=0,
            missing=0,
            repaired=0,
            debited=0,
            debit_usd=Decimal(0),
            truncated=False,
            repair_skipped=False,
        )
    except (_anthropic.APIError, SQLAlchemyError):
        log.exception("billing.reconcile_failed", managed_session_id=session_id)
    return None


def _retry_after(err: _anthropic.APIStatusError) -> float | None:
    raw = err.response.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _note_rate_limit(err: _anthropic.RateLimitError, *, session_id: str) -> None:
    """Start (or extend) the shared background cooldown from a 429's `retry-after`."""
    global _cooldown_until
    wait = min(_retry_after(err) or POLL_MAX_S, RATE_LIMIT_MAX_PAUSE_S)
    _cooldown_until = max(_cooldown_until, time.monotonic() + wait)
    log.warning("billing.headless_rate_limited", managed_session_id=session_id, retry_after_s=wait)


def _cooldown_left() -> float:
    """Whole seconds until background requests may resume (0 when they may now)."""
    left = _cooldown_until - time.monotonic()
    return float(math.ceil(left)) if left > 0 else 0.0


def _settled(result: ReconcileResult | None) -> bool:
    return result is not None and not result.truncated and not result.repair_skipped


# ---------------------------------------------------------------------------
# Waiting for turns to end
# ---------------------------------------------------------------------------


@dataclass
class _Segment:
    """What one turn's interval of the transcript holds."""

    end: TerminationReason | None
    calls: list[BetaManagedAgentsSpanModelRequestEndEvent]


def _idle_reason(event: object) -> TerminationReason:
    stop = getattr(getattr(event, "stop_reason", None), "type", None)
    if stop == "requires_action":
        return TerminationReason.REQUIRES_ACTION
    if stop == "retries_exhausted":
        return TerminationReason.UPSTREAM
    return TerminationReason.COMPLETED


class _Deadline(Exception):
    """The watcher's deadline came before (or during) an upstream read."""


async def _read[T](
    call: Callable[[], Awaitable[T]], *, deadline: float, clock: Callable[[], float]
) -> T:
    """One upstream read under the read limit, never past `deadline`."""
    remaining = deadline - clock()
    if remaining <= 0:
        raise _Deadline
    try:
        async with asyncio.timeout(min(REQUEST_TIMEOUT_S, remaining)):
            async with _limit("read"):
                return await call()
    except TimeoutError:
        if deadline - clock() <= 0:
            raise _Deadline from None
        raise


async def _walk(
    client: AsyncAnthropic,
    session_id: str,
    boundaries: set[str],
    *,
    deadline: float,
    clock: Callable[[], float],
) -> dict[str, _Segment]:
    """Walk the session newest first down to the oldest pending boundary.

    Newest first rather than filtered by the dispatcher's clock, so clock skew
    between this host and Managed Agents cannot hide a boundary. A turn's
    interval runs from its own `user.message` to the next one; its end is
    the first terminal event in that interval and its calls are the ones
    before that end. Returns the segment of every boundary reached.
    """
    found: dict[str, _Segment] = {}
    calls: list[BetaManagedAgentsSpanModelRequestEndEvent] = []
    end: TerminationReason | None = None
    page: str | None = None

    async def list_page(token: str | None) -> AsyncPageCursor[BetaManagedAgentsSessionEvent]:
        return await client.beta.sessions.events.list(
            session_id,
            order="desc",
            limit=100,
            types=_TURN_EVENT_TYPES,
            page=token if token is not None else omit,
        )

    for _ in range(MAX_TURN_PAGES):
        cursor = await _read(functools.partial(list_page, page), deadline=deadline, clock=clock)
        for event in cursor.data:
            if event.type == "user.message":
                if event.id in boundaries:
                    found[event.id] = _Segment(end=end, calls=list(reversed(calls)))
                    if len(found) == len(boundaries):
                        return found
                calls, end = [], None
            elif isinstance(event, BetaManagedAgentsSpanModelRequestEndEvent):
                calls.append(event)
            elif event.type in ("session.status_idle", "session.status_terminated"):
                # Walking newest first, the last one seen is the interval's first;
                # calls after it belong to no end this turn reached.
                end = (
                    _idle_reason(event)
                    if event.type == "session.status_idle"
                    else TerminationReason.SESSION_TERMINATED
                )
                calls = []
        if cursor.next_page is None:
            break
        page = cursor.next_page
    return found


def _finish(
    watch: _Watch,
    turn: _PendingTurn,
    reason: TerminationReason,
    calls: list[BetaManagedAgentsSpanModelRequestEndEvent] | None,
) -> None:
    """Write one turn's outcome; `calls=None` means its usage is unknown."""
    watch.pending.pop(turn.turn_event_id, None)
    observation = turn.observation
    if observation is None:
        return
    if calls is not None:
        session = watch.session
        observation.session_id = session.id
        observation.model_by_session[session.id] = session.agent.model.id
        observation.usage_available = True
        for event in calls:
            observation.note_usage(event, metered=watch.record is not None)
    observation.finish(reason=reason)


async def _poll(
    watch: _Watch, client: AsyncAnthropic, clock: Callable[[], float]
) -> BetaManagedAgentsSession:
    """Read the session's status; on idle or terminated, finish the turns that ended."""
    session_id = watch.session.id
    current = await _read(
        lambda: client.beta.sessions.retrieve(session_id), deadline=watch.deadline, clock=clock
    )
    if current.status not in ("idle", "terminated"):
        return current
    found = await _walk(
        client, session_id, set(watch.pending), deadline=watch.deadline, clock=clock
    )
    terminated = current.status == "terminated"
    for turn in list(watch.pending.values()):
        segment = found.get(turn.turn_event_id)
        if segment is not None and segment.end is not None:
            _finish(watch, turn, segment.end, segment.calls)
        elif terminated:
            _finish(
                watch,
                turn,
                TerminationReason.SESSION_TERMINATED,
                segment.calls if segment is not None else None,
            )
        elif segment is None:
            turn.unresolved_walks += 1
            if turn.unresolved_walks >= MAX_UNRESOLVED_WALKS:
                log.warning(
                    "billing.headless_boundary_unresolved",
                    managed_session_id=session_id,
                    turn_event_id=turn.turn_event_id,
                )
                _finish(watch, turn, TerminationReason.UNKNOWN, None)
        # else: the boundary is listed but its turn has not ended (a stale idle).
    return current


async def _run(
    watch: _Watch,
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]] | None,
) -> None:
    """Wait for the watch's turns, finish their outcomes, then bill until settled."""
    client = _background(anthropic)
    session_id = watch.session.id

    async def pause(seconds: float, *, interruptible: bool = True) -> None:
        if sleep is not None:
            await sleep(seconds)
            return
        if not interruptible:
            await asyncio.sleep(seconds)
            return
        # A turn dispatched meanwhile cuts the pause short.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await watch.wake.wait()

    honored = 0.0

    async def cool_down() -> None:
        """Sit out a background 429 cooldown; a new turn does not cut it short."""
        nonlocal honored
        if _cooldown_until > honored:
            left = _cooldown_left()
            honored = _cooldown_until
            if left > 0:
                await pause(left, interruptible=False)

    poll_delay = POLL_INITIAL_S
    failures = 0
    unbilled = False
    passes = 0
    retry_delay = SETTLE_RETRY_INITIAL_S
    while True:
        await cool_down()
        if watch.wake.is_set():
            watch.wake.clear()
            poll_delay = POLL_INITIAL_S
        if watch.pending:
            unbilled = True  # even a turn that expires below has calls to bill
        watch.expire(clock())
        if watch.pending:
            delay = poll_delay
            try:
                await _poll(watch, client, clock)
                failures = 0
            except _Deadline:
                log.warning("billing.headless_wait_expired", managed_session_id=session_id)
                watch.finalize()
            except _anthropic.NotFoundError:
                log.info("billing.headless_session_gone", managed_session_id=session_id)
                watch.finalize()
                unbilled = False
            except _anthropic.RateLimitError as err:
                _note_rate_limit(err, session_id=session_id)
                continue  # the loop's top sits out the cooldown
            except (_anthropic.APIError, TimeoutError):
                failures += 1
                if failures >= MAX_READ_FAILURES:
                    log.warning("billing.headless_wait_failed", managed_session_id=session_id)
                    watch.finalize()
            if watch.pending:
                await pause(min(delay, max(0.0, watch.deadline - clock())))
                poll_delay = min(poll_delay * POLL_BACKOFF, POLL_MAX_S)
                continue
        if unbilled and watch.record is not None:
            result = await _reconcile(anthropic, sessionmaker, session_id, watch.record)
            passes += 1
            if _settled(result):
                unbilled = False
                passes = 0
                retry_delay = SETTLE_RETRY_INITIAL_S
                _unsettled.pop(session_id, None)
            elif passes >= SETTLE_MAX_PASSES:
                log.error("billing.headless_settle_abandoned", managed_session_id=session_id)
                _mark_unsettled(session_id)
                unbilled = False
            else:
                if _cooldown_until > honored:
                    continue  # rate limited: the loop's top sits out the cooldown
                if result is None:
                    await pause(retry_delay)
                    retry_delay = min(retry_delay * 2, SETTLE_RETRY_MAX_S)
                else:
                    await pause(SETTLE_CONTINUE_S)
                continue
        unbilled = False
        if not watch.pending:
            return
        # A turn was dispatched on the session while it was being billed.


async def settle_turn(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    turn: DispatchedTurn,
    markup: Decimal,
    observation: TurnObservation | None = None,
    timeout_s: float = TURN_CEILING_S,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for `turn` to end, finish its outcome, and bill the session until settled.

    Bills even when the wait gave up (the session gone excepted): passes debit
    whatever is listed by then and resume from their durable progress.
    """
    deadline = clock() + timeout_s
    watch = _Watch(
        session=turn.session,
        record=headless_recorder(
            sessionmaker,
            turn.session,
            tenant_id=turn.tenant_id,
            platform_user_id=turn.platform_user_id,
            markup=markup,
        ),
        deadline=deadline,
    )
    watch.add(_PendingTurn(turn.turn_event_id, observation, deadline=deadline))
    try:
        await _run(
            watch,
            anthropic=anthropic,
            sessionmaker=sessionmaker,
            clock=clock,
            sleep=sleep,
        )
    finally:
        watch.finalize()


def watch_turn(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    turn: DispatchedTurn,
    markup: Decimal,
    observation: TurnObservation | None = None,
) -> None:
    """Have the session's watcher settle a dispatched turn; returns at once.

    Joins the session's existing watcher if there is one. With `MAX_WATCHERS`
    sessions already watched the turn is not watched: its outcome is
    finished now as `unknown` and the session is marked unsettled, so the
    poll fallback (or a later turn, or the sweep) bills it.
    """
    session_id = turn.session.id
    clock = time.monotonic
    record = headless_recorder(
        sessionmaker,
        turn.session,
        tenant_id=turn.tenant_id,
        platform_user_id=turn.platform_user_id,
        markup=markup,
    )
    pending = _PendingTurn(turn.turn_event_id, observation, deadline=clock() + TURN_CEILING_S)
    existing = _watches.get(session_id)
    if existing is not None:
        existing.add(pending)
        existing.record = record
        existing.wake.set()
        return
    if len(_watches) >= MAX_WATCHERS:
        log.warning("billing.headless_watchers_full", managed_session_id=session_id)
        if observation is not None:
            observation.finish(reason=TerminationReason.UNKNOWN)
        _mark_unsettled(session_id)
        return
    watch = _Watch(session=turn.session, record=record, deadline=pending.deadline)
    watch.add(pending)
    _watches[session_id] = watch

    async def run() -> None:
        try:
            await _run(
                watch,
                anthropic=anthropic,
                sessionmaker=sessionmaker,
                clock=clock,
                sleep=None,
            )
        except Exception:
            # The task boundary: billing in the background must never take the
            # server down. The session is left for the poll fallback.
            log.exception("billing.headless_settle_failed", managed_session_id=session_id)
            _mark_unsettled(session_id)

    task = asyncio.create_task(run(), name="billing.headless_settle")
    _watch_tasks[session_id] = task

    def done(_t: asyncio.Task[None]) -> None:
        # Runs however the task ended, including a cancel before its first
        # step: no handed-off outcome is left unwritten.
        if _watches.get(session_id) is watch:
            del _watches[session_id]
        if _watch_tasks.get(session_id) is _t:
            del _watch_tasks[session_id]
        if watch.pending:
            _mark_unsettled(session_id)
        watch.finalize()

    task.add_done_callback(done)


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
    """Queue catch-up passes for a session seen idle or terminated; returns at once.

    Returns whether passes were queued: not while the session is running,
    has a pending watcher (which settles it), already has a queued pass, was
    caught up in the last `CATCH_UP_INTERVAL_S` (`CATCH_UP_RETRY_S` when
    unsettled), is not billed, or `MAX_CATCH_UPS` are already queued.
    """
    session_id = session.id
    if session.status not in ("idle", "terminated"):
        return False
    if session_id in _watches or session_id in _catch_ups or len(_catch_ups) >= MAX_CATCH_UPS:
        return False
    if _cooldown_left() > 0:
        return False  # a background 429 cooldown is running
    now = clock()
    last = _last_catch_up.get(session_id)
    gap = CATCH_UP_RETRY_S if session_id in _unsettled else CATCH_UP_INTERVAL_S
    if last is not None and now - last < gap:
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
    _last_catch_up[session_id] = now
    _remember(_last_catch_up, session_id)

    async def run() -> None:
        try:
            delay = SETTLE_RETRY_INITIAL_S
            for _ in range(CATCH_UP_MAX_PASSES):
                result = await _reconcile(anthropic, sessionmaker, session_id, record)
                if _settled(result):
                    _unsettled.pop(session_id, None)
                    return
                if _cooldown_left() > 0:
                    break  # rate limited: the next poll after the cooldown retries
                if result is None:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, SETTLE_RETRY_MAX_S)
                else:
                    await asyncio.sleep(SETTLE_CONTINUE_S)
                if _cooldown_left() > 0:
                    break  # another background read was rate limited meanwhile
            _mark_unsettled(session_id)
        except Exception:
            log.exception("billing.headless_catch_up_failed", managed_session_id=session_id)
            _mark_unsettled(session_id)

    task = asyncio.create_task(run(), name="billing.headless_catch_up")
    _catch_ups[session_id] = task

    def done(t: asyncio.Task[None]) -> None:
        if _catch_ups.get(session_id) is t:
            del _catch_ups[session_id]

    task.add_done_callback(done)
    return True


def _all_tasks() -> list[asyncio.Task[None]]:
    loop = asyncio.get_running_loop()
    tasks = [*_watch_tasks.values(), *_catch_ups.values()]
    return [t for t in tasks if t.get_loop() is loop]


async def drain() -> None:
    """Await every pending watcher and catch-up (a test and shutdown barrier)."""
    while tasks := _all_tasks():
        await asyncio.gather(*tasks, return_exceptions=True)


async def shutdown() -> None:
    """Cancel pending work on server shutdown.

    Every watched turn's outcome is written as `unknown` (including a watcher
    cancelled before it started); its calls are billed by the next catch-up
    on the session.
    """
    global _cooldown_until
    tasks = _all_tasks()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for watch in list(_watches.values()):
        watch.finalize()
    _cooldown_until = 0.0
