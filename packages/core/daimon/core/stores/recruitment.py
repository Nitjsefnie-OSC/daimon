"""Tenant-bound encrypted recruitment ledger; callers never see ORM rows."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from cryptography.fernet import MultiFernet
from daimon.core._models import (
    RecruitmentConfigRecord,
    RecruitmentMessageRecord,
    RecruitmentOutboxRecord,
    RecruitmentThreadRecord,
    ThreadSession,
)
from daimon.core.recruitment import Outbox, RecruitmentConfig, SourceMessage, ThreadState
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class RecruitmentBusy(Exception):
    pass


class RecruitmentStore:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], fernet: MultiFernet, tenant_id: UUID
    ) -> None:
        self.sessions, self.fernet, self.tenant_id = sessions, fernet, tenant_id

    def seal(self, value: BaseModel) -> bytes:
        return self.fernet.encrypt(value.model_dump_json().encode())

    @asynccontextmanager
    async def claim(self, thread_id: str) -> AsyncIterator[None]:
        async with self.sessions.begin() as session:
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"recruitment:{self.tenant_id}:{thread_id}"},
            )
            if not acquired:
                raise RecruitmentBusy
            yield

    async def configure(self, config: RecruitmentConfig) -> None:
        from daimon.core.ma_identity import derive_tenant_uuid

        if derive_tenant_uuid(platform="discord", workspace_id=config.guild_id) != self.tenant_id:
            raise ValueError("tenant_guild_mismatch")
        async with self.claim("config"), self.sessions.begin() as session:
            old = await session.get(RecruitmentConfigRecord, self.tenant_id)
            if (
                old
                and config != RecruitmentConfig.model_validate(old.config)
                and config.revision <= int(old.config["revision"])
            ):
                raise ValueError("configuration_revision_must_increase")
            await session.execute(
                insert(RecruitmentConfigRecord)
                .values(tenant_id=self.tenant_id, config=config.model_dump(mode="json"))
                .on_conflict_do_update(
                    index_elements=["tenant_id"], set_={"config": config.model_dump(mode="json")}
                )
            )

    async def configuration(self) -> RecruitmentConfig | None:
        async with self.sessions() as session:
            row = await session.get(RecruitmentConfigRecord, self.tenant_id)
            return RecruitmentConfig.model_validate(row.config) if row else None

    async def has_provider_session(self, thread_id: str) -> bool:
        async with self.sessions() as session:
            return bool(
                await session.scalar(
                    select(ThreadSession.id)
                    .where(
                        ThreadSession.tenant_id == self.tenant_id,
                        ThreadSession.thread_id == thread_id,
                    )
                    .limit(1)
                )
            )

    async def enrolled(self, thread_id: str) -> bool:
        async with self.sessions() as session:
            return (
                await session.get(RecruitmentThreadRecord, (self.tenant_id, thread_id)) is not None
            )

    async def list_threads(self) -> list[str]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(RecruitmentThreadRecord.thread_id).where(
                        RecruitmentThreadRecord.tenant_id == self.tenant_id
                    )
                )
            )

    async def get(self, thread_id: str) -> ThreadState | None:
        async with self.sessions() as session:
            row = await session.get(RecruitmentThreadRecord, (self.tenant_id, thread_id))
            if row is None:
                return None
            if row.encrypted_state is None:
                return ThreadState(thread_id=thread_id, parent_id=row.parent_id, phase="expired")
            return ThreadState.model_validate_json(self.fernet.decrypt(row.encrypted_state))

    async def save(
        self,
        state: ThreadState,
        config: RecruitmentConfig,
        *,
        activity: bool = False,
        outbox: Outbox | None = None,
        update_outbox: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            values = dict(
                tenant_id=self.tenant_id,
                thread_id=state.thread_id,
                parent_id=state.parent_id,
                phase=state.phase,
                revision=state.revision,
                encrypted_state=self.seal(state),
                raw_expires_at=now + timedelta(days=config.raw_retention_days),
                expires_at=now + timedelta(days=config.retention_days),
            )
            changes = {
                k: v
                for k, v in values.items()
                if k in {"phase", "revision", "encrypted_state"}
                or activity
                and k in {"raw_expires_at", "expires_at"}
            }
            await session.execute(
                insert(RecruitmentThreadRecord)
                .values(**values)
                .on_conflict_do_update(index_elements=["tenant_id", "thread_id"], set_=changes)
            )
            if outbox:
                if update_outbox:
                    await session.execute(
                        update(RecruitmentOutboxRecord)
                        .where(
                            RecruitmentOutboxRecord.tenant_id == self.tenant_id,
                            RecruitmentOutboxRecord.id == outbox.id,
                            RecruitmentOutboxRecord.thread_id == state.thread_id,
                        )
                        .values(state=outbox.state, encrypted_request=self.seal(outbox))
                    )
                else:
                    await session.execute(
                        insert(RecruitmentOutboxRecord).values(
                            tenant_id=self.tenant_id,
                            id=outbox.id,
                            thread_id=outbox.thread_id,
                            revision=outbox.revision,
                            stable_key=outbox.key,
                            state=outbox.state,
                            encrypted_request=self.seal(outbox),
                        )
                    )

    async def messages(self, thread_id: str) -> list[SourceMessage]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(RecruitmentMessageRecord).where(
                    RecruitmentMessageRecord.tenant_id == self.tenant_id,
                    RecruitmentMessageRecord.thread_id == thread_id,
                    RecruitmentMessageRecord.encrypted_source.is_not(None),
                )
            )
            return sorted(
                [
                    SourceMessage.model_validate_json(self.fernet.decrypt(r.encrypted_source))
                    for r in rows
                    if r.encrypted_source
                ],
                key=lambda m: int(m.id),
            )

    async def ingest(self, thread_id: str, message: SourceMessage) -> bool:
        async with self.sessions.begin() as session:
            row = await session.get(
                RecruitmentMessageRecord, (self.tenant_id, thread_id, message.id)
            )
            # Ignore stale gateway events; history rereads retain the actual edited time.
            if row:
                if row.encrypted_source is None:
                    return False
                old = SourceMessage.model_validate_json(self.fernet.decrypt(row.encrypted_source))
                if old.observed_at > message.observed_at or old == message:
                    return False
            thread = await session.get(RecruitmentThreadRecord, (self.tenant_id, thread_id))
            if not thread or not thread.encrypted_state:
                raise LookupError("thread_not_enrolled")
            state = ThreadState.model_validate_json(self.fernet.decrypt(thread.encrypted_state))
            state.revision, state.error, state.card_revision = uuid4().hex, "refresh_required", ""
            thread.revision, thread.encrypted_state = state.revision, self.seal(state)
            await session.execute(
                insert(RecruitmentMessageRecord)
                .values(
                    tenant_id=self.tenant_id,
                    thread_id=thread_id,
                    message_id=message.id,
                    revision=message.observed_at.isoformat(),
                    encrypted_source=self.seal(message),
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "thread_id", "message_id"],
                    set_={
                        "revision": message.observed_at.isoformat(),
                        "encrypted_source": self.seal(message),
                    },
                )
            )
            return True

    async def outbox(self, id: str) -> Outbox | None:
        async with self.sessions() as session:
            row = await session.get(RecruitmentOutboxRecord, (self.tenant_id, id))
            return (
                Outbox.model_validate_json(self.fernet.decrypt(row.encrypted_request))
                if row and row.encrypted_request
                else None
            )

    async def save_outbox(self, item: Outbox) -> None:
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(RecruitmentOutboxRecord)
                .where(
                    RecruitmentOutboxRecord.tenant_id == self.tenant_id,
                    RecruitmentOutboxRecord.id == item.id,
                    RecruitmentOutboxRecord.thread_id == item.thread_id,
                    RecruitmentOutboxRecord.stable_key == item.key,
                )
                .values(state=item.state, encrypted_request=self.seal(item))
            )
            if cast(CursorResult[tuple[object, ...]], result).rowcount != 1:
                raise LookupError("outbox_not_found")

    async def purge(self, now: datetime) -> None:
        async with self.sessions.begin() as session:
            raw_expired = select(RecruitmentThreadRecord.thread_id).where(
                RecruitmentThreadRecord.tenant_id == self.tenant_id,
                RecruitmentThreadRecord.raw_expires_at <= now,
            )
            expired = select(RecruitmentThreadRecord.thread_id).where(
                RecruitmentThreadRecord.tenant_id == self.tenant_id,
                RecruitmentThreadRecord.expires_at <= now,
            )
            await session.execute(
                update(RecruitmentMessageRecord)
                .where(
                    RecruitmentMessageRecord.tenant_id == self.tenant_id,
                    RecruitmentMessageRecord.thread_id.in_(raw_expired),
                )
                .values(encrypted_source=None)
            )
            await session.execute(
                update(RecruitmentOutboxRecord)
                .where(
                    RecruitmentOutboxRecord.tenant_id == self.tenant_id,
                    RecruitmentOutboxRecord.thread_id.in_(expired),
                )
                .values(encrypted_request=None, state="expired")
            )
            await session.execute(
                update(RecruitmentThreadRecord)
                .where(
                    RecruitmentThreadRecord.tenant_id == self.tenant_id,
                    RecruitmentThreadRecord.thread_id.in_(expired),
                )
                .values(encrypted_state=None, phase="expired")
            )


async def configured_tenants(sessions: async_sessionmaker[AsyncSession]) -> list[UUID]:
    """Operator sweep enumerates configs; all subsequent operations use a bound store."""
    async with sessions() as session:
        return list(await session.scalars(select(RecruitmentConfigRecord.tenant_id)))


async def is_recruitment_source(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    thread_id: str,
    *,
    title: str = "",
    parent_id: str = "",
) -> bool:
    """Single privacy predicate, independent of feature enablement and crypto keys."""
    from daimon.core.recruitment import enrollment_source

    async with sessions() as session:
        if await session.get(RecruitmentThreadRecord, (tenant_id, thread_id)):
            return True
        row = await session.get(RecruitmentConfigRecord, tenant_id)
        return bool(
            row
            and enrollment_source(title, parent_id, RecruitmentConfig.model_validate(row.config))
        )
