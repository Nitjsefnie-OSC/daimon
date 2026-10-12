"""Worker fleets: an orchestrator turn's fan-out, kept durable.

A fleet run is the orchestrator's inbox. Each worker is one more binding slot
in the neutral state store, so its session, fenced lease, idempotent spawn
operation, journal and usage are the store's existing ones. The fleet tables
(`daimon.core.stores.fleet`) hold only what the store cannot: the run and its
workers, their specs, budgets, deadlines and status, and a pointer to each
worker's result, which itself lives in the worker's journal.

Every status change is a compare-and-swap on the row's `version`, along
`WORKER_TRANSITIONS` / `RUN_TRANSITIONS`; a lost race or a refused move raises
`FleetConflict`. Concurrency is a pool of its own: `WorkerPool` caps one
process, and `claim_worker` caps a tenant from the durable rows, so a fan-out
never takes the chat turns' slots and a restart cannot leak a slot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal

from mux.contracts.events import Event
from mux.contracts.ids import ChannelRef, ResourceRef, ThreadRef
from mux.state.journal import JournalEntry
from mux.state.lease import Lease, Slot
from mux.state.store import StateStore
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

type RunStatus = Literal["running", "collecting", "completed", "cancelled", "failed"]
type WorkerStatus = Literal[
    "pending",
    "starting",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "budget_exceeded",
]

TERMINAL: Final[frozenset[WorkerStatus]] = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "budget_exceeded"}
)
"""A worker in one of these never moves again."""

WORKER_TRANSITIONS: Final[Mapping[WorkerStatus, frozenset[WorkerStatus]]] = {
    "pending": frozenset({"starting", "cancelled"}),
    # Back to pending only through recovery, for a turn that was never sent.
    "starting": frozenset({"running", "pending", "failed", "cancelled", "timed_out"}),
    "running": frozenset(
        {"succeeded", "failed", "cancelled", "timed_out", "budget_exceeded", "pending"}
    ),
}

RUN_TRANSITIONS: Final[Mapping[RunStatus, frozenset[RunStatus]]] = {
    "running": frozenset({"collecting", "cancelled", "failed"}),
    "collecting": frozenset({"completed", "cancelled", "failed"}),
}

EXCERPT_LIMIT: Final = 4096


class FleetConflict(Exception):
    """A lost `version` compare-and-swap, or a status move the transitions refuse."""


@dataclass(frozen=True)
class ResultPointer:
    """Where a worker's result is: one journal event, with its digest."""

    session_id: str
    sequence: int
    digest: str
    excerpt: str


@dataclass(frozen=True)
class FleetRun:
    id: uuid.UUID
    tenant_id: uuid.UUID
    orchestrator_account_id: uuid.UUID
    orchestrator_session_id: str
    orchestrator_binding_id: str
    platform: str
    channel_id: str
    thread_id: str
    status: RunStatus
    total_budget_usd: Decimal
    reserved_usd: Decimal
    observed_usd: Decimal
    deadline_at: datetime
    max_workers: int
    quorum: int | None
    version: int


@dataclass(frozen=True)
class FleetWorker:
    id: uuid.UUID
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    ordinal: int
    role: str
    spec: Mapping[str, JsonValue]
    spec_digest: str
    backend: str
    profile: str
    model: str
    binding_id: str | None
    status: WorkerStatus
    budget_usd: Decimal
    reserved_usd: Decimal
    deadline_at: datetime
    max_turns: int
    turns_used: int
    attempts: int
    reviewer_of: uuid.UUID | None
    result_contract: Mapping[str, JsonValue] | None
    result: ResultPointer | None
    error_code: str | None
    version: int


def check_worker_move(current: WorkerStatus, target: WorkerStatus) -> None:
    if target not in WORKER_TRANSITIONS.get(current, frozenset()):
        raise FleetConflict(f"a {current} worker cannot become {target}")


def check_run_move(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS.get(current, frozenset()):
        raise FleetConflict(f"a {current} run cannot become {target}")


def worker_slot(run: FleetRun, worker: FleetWorker) -> Slot:
    """The worker's private binding slot: its own thread under the orchestrator's
    channel, owned by the orchestrator's account. Never the shared slot."""
    channel = ChannelRef(
        tenant_id=str(run.tenant_id), platform=run.platform, channel_id=run.channel_id
    )
    return Slot(
        thread=ThreadRef(channel=channel, thread_id=f"fleet/{run.id}/{worker.ordinal}"),
        account_id=str(run.orchestrator_account_id),
    )


def spawn_operation_key(run_id: uuid.UUID, ordinal: int) -> str:
    """The state store operation key that makes a worker's spawn idempotent."""
    return f"fleet:{run_id}:spawn:{ordinal}"


def result_source_key(worker_id: uuid.UUID) -> str:
    """The journal source key of a worker's result entry (deduplicated per worker)."""
    return f"fleet:{worker_id}:result"


def run_settled(run: FleetRun, workers: Sequence[FleetWorker]) -> bool:
    """Whether the orchestrator should be woken: every worker done, or its quorum."""
    done = sum(1 for worker in workers if worker.status in TERMINAL)
    if run.quorum is not None:
        return done >= run.quorum
    return bool(workers) and done == len(workers)


def result_digest(payload: Mapping[str, JsonValue]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _excerpt(payload: Mapping[str, JsonValue]) -> str:
    """The result's text (a message's text blocks), else its JSON, cut for the card."""
    text = payload.get("text")
    if isinstance(text, str):
        return text[:EXCERPT_LIMIT]
    content = payload.get("content")
    blocks = content if isinstance(content, list) else []
    parts = [
        part
        for block in blocks
        if isinstance(block, dict) and isinstance(part := block.get("text"), str)
    ]
    body = "".join(parts) if parts else json.dumps(payload, sort_keys=True)
    return body[:EXCERPT_LIMIT]


async def append_result(
    store: StateStore,
    session: ResourceRef,
    worker: FleetWorker,
    event: Event,
    *,
    lease: Lease,
    cursor: str,
    now: datetime,
) -> ResultPointer:
    """Append the worker's result to its own journal, once, under its lease.

    The entry's source key is per worker, so a retry or a lease successor that
    appends it again is a journal duplicate; the pointer then names the event
    already stored.
    """
    appended = await store.append_events(
        session,
        [JournalEntry(source_key=result_source_key(worker.id), event=event)],
        fence=lease,
        cursor=cursor,
        now=now,
    )
    stored = appended.appended[0] if appended.appended else await _stored(store, session, event)
    payload = dict(stored.payload)
    return ResultPointer(
        session_id=session.id,
        sequence=stored.sequence,
        digest=result_digest(payload),
        excerpt=_excerpt(payload),
    )


async def _stored(store: StateStore, session: ResourceRef, event: Event) -> Event:
    after = -1
    while True:
        page = await store.read_events(session.id, after=after)
        if not page:
            raise FleetConflict(f"result event {event.id} is not in session {session.id}")
        for stored in page:
            if stored.id == event.id:
                return stored
        after = page[-1].sequence


class WorkerPool:
    """The process's cap over worker turns, in front of the durable tenant cap.

    `claim` takes a process slot, then a tenant slot through `claim_worker`; a
    refused tenant claim gives the process slot back. The caller `release`s the
    process slot when the worker's turn ends, on every path. The tenant slot needs
    no release: it is the worker row's own status.
    """

    def __init__(self, *, max_concurrent_workers: int, max_concurrent_workers_per_tenant: int):
        self._slots = asyncio.Semaphore(max_concurrent_workers)
        self._tenant_cap = max_concurrent_workers_per_tenant

    async def claim(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> FleetWorker | None:
        """The run's next pending worker, now `starting`, or None if no room."""
        from daimon.core.stores.fleet import claim_worker

        if self._slots.locked():
            return None
        await self._slots.acquire()
        try:
            worker = await claim_worker(
                sessionmaker, tenant_id=tenant_id, run_id=run_id, cap=self._tenant_cap
            )
        except BaseException:
            self._slots.release()
            raise
        if worker is None:
            self._slots.release()
        return worker

    def release(self) -> None:
        self._slots.release()
