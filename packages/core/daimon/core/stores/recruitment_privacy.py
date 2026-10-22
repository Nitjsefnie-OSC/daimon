"""One recruitment source policy shared by provider serializers and MCP output.

Admission's negative cache is only an optimization. Actual turn dispatch and
provider/MCP serialization load an authoritative, task-scoped snapshot. Thus a
config/enrollment committed by another process cannot slip through a stale
negative admission cache. No candidate content is cached here.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID
from weakref import WeakKeyDictionary

from daimon.core._models import RecruitmentConfigRecord, RecruitmentThreadRecord
from daimon.core.recruitment import RecruitmentConfig, enrollment_source
from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class SourceChannel:
    id: str
    guild_id: str
    parent_id: str = ""
    title: str = ""
    is_thread: bool = False


type ResolveChannel = Callable[[str], Awaitable[SourceChannel]]


@dataclass
class RecruitmentSources:
    config: RecruitmentConfig | None = None
    threads: frozenset[str] = frozenset()
    resolved: dict[str, bool] = field(default_factory=dict[str, bool])

    @property
    def active(self) -> bool:
        return self.config is not None or bool(self.threads)

    async def thread(self, id: str, resolve: ResolveChannel) -> bool:
        if id in self.threads:
            return True
        if self.config is None:
            return False
        if id not in self.resolved:
            try:
                live = await resolve(id)
                self.resolved[id] = live.guild_id != self.config.guild_id or (
                    live.is_thread and enrollment_source(live.title, live.parent_id, self.config)
                )
            except Exception:
                # A search index, gateway cache or inaccessible source cannot
                # establish that a qualifying thread is safe to serialize.
                self.resolved[id] = True
        return self.resolved[id]

    async def message(
        self,
        channel: SourceChannel,
        message_id: str,
        resolve: ResolveChannel,
        *,
        starter_thread_id: str | None = None,
        notice_thread_id: str | None = None,
    ) -> bool:
        if not self.active:
            return False
        if message_id in self.threads or channel.id in self.threads:
            return True
        if channel.is_thread and await self.thread(channel.id, resolve):
            return True
        for id in (starter_thread_id, notice_thread_id):
            if id is not None and await self.thread(id, resolve):
                return True
        return False


_INDEX: WeakKeyDictionary[async_sessionmaker[AsyncSession], tuple[float, frozenset[UUID]]] = (
    WeakKeyDictionary()
)
_TASKS: WeakKeyDictionary[
    asyncio.Task[object], dict[tuple[async_sessionmaker[AsyncSession], UUID], RecruitmentSources]
] = WeakKeyDictionary()


def protect_tenant(sessions: async_sessionmaker[AsyncSession], tenant_id: UUID) -> None:
    """Invalidate local negatives immediately on config/enrollment writes."""
    _, tenants = _INDEX.get(sessions, (0.0, frozenset()))
    _INDEX[sessions] = (0.0, tenants | {tenant_id})
    for cached in _TASKS.values():
        cached.pop((sessions, tenant_id), None)


async def privacy_active(sessions: async_sessionmaker[AsyncSession], tenant_id: UUID) -> bool:
    stamp, tenants = _INDEX.get(sessions, (0.0, frozenset()))
    if tenant_id in tenants:
        return True  # Sticky positives retain privacy after disable/erasure.
    if time.monotonic() - stamp >= 5:
        async with sessions() as session:
            known = frozenset(
                await session.scalars(
                    union(
                        select(RecruitmentConfigRecord.tenant_id),
                        select(RecruitmentThreadRecord.tenant_id),
                    )
                )
            )
        tenants = tenants | known
        _INDEX[sessions] = (time.monotonic(), tenants)
    return tenant_id in tenants


async def sources_for_task(
    sessions: async_sessionmaker[AsyncSession], tenant_id: UUID, *, fresh: bool = False
) -> RecruitmentSources:
    task = asyncio.current_task()
    assert task is not None
    cache = _TASKS.setdefault(task, {})
    key = (sessions, tenant_id)
    if fresh or key not in cache:
        async with sessions() as session:
            row = await session.get(RecruitmentConfigRecord, tenant_id)
            threads = frozenset(
                await session.scalars(
                    select(RecruitmentThreadRecord.thread_id).where(
                        RecruitmentThreadRecord.tenant_id == tenant_id
                    )
                )
            )
        cache[key] = RecruitmentSources(
            RecruitmentConfig.model_validate(row.config) if row else None, threads
        )
        if cache[key].active:
            stamp, known = _INDEX.get(sessions, (0.0, frozenset()))
            _INDEX[sessions] = (stamp, known | {tenant_id})
    return cache[key]
