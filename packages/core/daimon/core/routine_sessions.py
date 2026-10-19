"""Cancellation-independent ownership of private routine allocation obligations.

The context follows the normal turn's assembly and recovery allocations. Its
registry belongs to the dispatcher, outside the fire and its timeout. Every
provider allocation first commits its own intent, even inside a bind transaction.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TypeVar

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.core.ma import stop_routine_session
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import legacy_iter
from daimon.core.stores import routine_sessions as store
from daimon.core.stores.domain import RoutineSessionRow
from mux.drivers.anthropic.core_admin import CoreAdmin
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
T = TypeVar("T")
current_lifecycle: ContextVar[RoutineFire | None] = ContextVar("routine_lifecycle", default=None)


class RoutineSessionSupervisor:
    """Own finalization and stop tasks until resource teardown."""

    def __init__(
        self, sm: async_sessionmaker[AsyncSession], client: AsyncAnthropic, *, owner_key: int
    ):
        self.sm = sm
        self.client = client
        self.owner_key = owner_key
        self._tasks: set[asyncio.Task[object]] = set()
        self._stops: dict[uuid.UUID, asyncio.Task[object]] = {}
        self._fires: set[RoutineFire] = set()
        self.completions: dict[uuid.UUID, asyncio.Task[None]] = {}

    def own(self, work: Coroutine[object, object, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(work)
        self._tasks.add(task)

        def done(task: asyncio.Task[T]) -> None:
            self._tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.error("routine.lifecycle_failed", error=str(task.exception())[:500])

        task.add_done_callback(done)
        return task

    @contextmanager
    def fire(self, routine_id: uuid.UUID) -> Iterator[RoutineFire]:
        fire = RoutineFire(self, routine_id)
        self._fires.add(fire)
        token = current_lifecycle.set(fire)
        try:
            yield fire
        finally:
            current_lifecycle.reset(token)
            self._fires.discard(fire)
            # Includes a bind failure before the caller ever got a session ID.
            for intent_id in fire.intents:
                self.submit_stop(intent_id)

    def submit_stop(self, intent_id: uuid.UUID) -> asyncio.Task[object]:
        task = self._stops.get(intent_id)
        if task is None or task.done():
            task = self.own(self._attempt(intent_id))
            self._stops[intent_id] = task

            def done(finished: asyncio.Task[object]) -> None:
                if self._stops.get(intent_id) is finished:
                    del self._stops[intent_id]

            task.add_done_callback(done)
        return task

    async def _attempt(self, intent_id: uuid.UUID) -> None:
        try:
            # Five seconds includes completion joins, claim, provider I/O and persistence.
            async with asyncio.timeout(5):
                completing = self.completions.get(intent_id)
                if completing is not None:
                    # A failed completion write leaves the intent pending.
                    with suppress(Exception):
                        await asyncio.shield(completing)
                    self.completions.pop(intent_id, None)
                async with self.sm.begin() as db:
                    row = await store.begin_attempt(db, intent_id, owner_key=self.owner_key)
                if row is None or row.state in store.TERMINAL:
                    return
                failure = "Provider cessation was not confirmed."
                try:
                    async with asyncio.timeout(3):
                        stopped = await self._discover_and_stop(row)
                except Exception as error:
                    stopped = False
                    failure = f"{type(error).__name__}: {error}"[:500]
                async with self.sm.begin() as db:
                    await store.transition(
                        db,
                        intent_id,
                        "stopped"
                        if stopped
                        else "stop_unresolved"
                        if row.stop_attempts >= 3
                        else "stopping",
                        failure=None if stopped else failure,
                    )
        except Exception as error:
            # The intent/attempt is already committed. Recovery owns the retry.
            log.warning("routine.stop_pending", intent_id=str(intent_id), error=str(error)[:500])

    async def _discover_and_stop(self, row: RoutineSessionRow) -> bool:
        scope = resource_scope(
            tenant_id=str(row.tenant_id),
            account_id=str(row.account_id),
            authorization_id="dm-routine-stop",
        )
        ids: set[str] = set()
        if row.ma_session_id is not None:
            ids.add(row.ma_session_id)
            ids.update(row.discovered_session_ids)
        else:
            backend = managed_agents(
                self.client, scope=scope, resources=frozenset({("agent", row.ma_agent_id)})
            )
            port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)
            async for record in legacy_iter(
                port.sessions_for_agent(
                    scope, resource_ref(backend, "agent", row.ma_agent_id, scope=scope)
                )
            ):
                metadata = record.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                if (
                    metadata.get("daimon_routine_session") == str(row.id)
                    and metadata.get("daimon_tenant") == str(row.tenant_id)
                    and metadata.get("daimon_account") == str(row.account_id)
                ):
                    session_id = record.get("id")
                    if isinstance(session_id, str):
                        ids.add(session_id)
            # Absence after an ambiguous create is never evidence of cessation.
            if not ids:
                return False
            # Do not truncate discovery to a single match; all matches must stop.
            async with self.sm.begin() as db:
                await store.transition(
                    db,
                    row.id,
                    "stopping",
                    session_id=sorted(ids)[0],
                    discovered_session_ids=tuple(sorted(ids)),
                )
        results = [
            await stop_routine_session(self.client, session_id=sid, scope=scope) for sid in ids
        ]
        return all(results)

    async def recover(self) -> None:
        """Retry stale owners and our own stopping rows, never a live fire."""
        active = {intent for fire in self._fires for intent in fire.intents}
        async with self.sm() as db:
            rows = await store.pending(db)
        tasks = [
            self.submit_stop(row.id)
            for row in rows
            if row.id not in active or row.state == "stopping"
        ]
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks))

    async def drain(self) -> None:
        """Join all owned work, including stops submitted by finalizers."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def abort_pending(self) -> None:
        """At teardown, persist obligations already exist; cancel and join I/O."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(eq=False)
class RoutineFire:
    supervisor: RoutineSessionSupervisor
    routine_id: uuid.UUID
    fire_id: uuid.UUID = field(default_factory=uuid.uuid4)
    intents: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    sessions: dict[str, uuid.UUID] = field(default_factory=dict[str, uuid.UUID])

    async def allocate(
        self,
        call: Callable[[], Awaitable[BetaManagedAgentsSession]],
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        agent_id: str,
        metadata: dict[str, str],
    ) -> BetaManagedAgentsSession:
        async with self.supervisor.sm.begin() as db:
            row = await store.record(
                db,
                routine_id=self.routine_id,
                fire_id=self.fire_id,
                tenant_id=tenant_id,
                account_id=account_id,
                ma_agent_id=agent_id,
                owner_key=self.supervisor.owner_key,
            )
        self.intents.append(row.id)
        metadata["daimon_routine_session"] = str(row.id)
        created = await call()
        self.sessions[created.id] = row.id
        async with self.supervisor.sm.begin() as db:
            await store.transition(db, row.id, "session_created", session_id=created.id)
        return created

    async def mapped(self, session_id: str, mapping_id: uuid.UUID) -> None:
        intent = self.sessions[session_id]
        async with self.supervisor.sm.begin() as db:
            await store.transition(db, intent, "session_created", mapping_id=mapping_id)

    async def running(self, session_id: str) -> None:
        async with self.supervisor.sm.begin() as db:
            await store.transition(db, self.sessions[session_id], "running")

    async def completed(self, session_id: str) -> None:
        async def finalize() -> None:
            async with self.supervisor.sm.begin() as db:
                await store.transition(db, self.sessions[session_id], "completed")

        task = self.supervisor.own(finalize())
        self.supervisor.completions[self.sessions[session_id]] = task
        try:
            await asyncio.shield(task)
        finally:
            # Keep a pending completion visible to a fire's stop callback.
            if task.done():
                self.supervisor.completions.pop(self.sessions[session_id], None)

    async def stop(self, session_id: str) -> None:
        await asyncio.shield(self.supervisor.submit_stop(self.sessions[session_id]))
