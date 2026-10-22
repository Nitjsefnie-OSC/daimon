"""Tenant-bound encrypted recruitment ledger; callers never see ORM rows."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from cryptography.fernet import MultiFernet
from daimon.core._models import (
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
        self,
        sessions: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        tenant_id: UUID,
        config: RecruitmentConfig | None = None,
    ) -> None:
        self.sessions, self.fernet, self.tenant_id = sessions, fernet, tenant_id
        if config is not None:
            from daimon.core.ma_identity import derive_tenant_uuid

            if derive_tenant_uuid(platform="discord", workspace_id=config.guild_id) != tenant_id:
                raise ValueError("tenant_guild_mismatch")
        self.config = config
        self._claim: ContextVar[tuple[str, str] | None] = ContextVar(
            "recruitment_claim", default=None
        )

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
            token = uuid4().hex
            async with self.sessions.begin() as writer:
                await writer.execute(
                    update(RecruitmentThreadRecord)
                    .where(
                        RecruitmentThreadRecord.tenant_id == self.tenant_id,
                        RecruitmentThreadRecord.thread_id == thread_id,
                    )
                    .values(claim_token=token)
                )
            context = self._claim.set((thread_id, token))
            try:
                yield
            finally:
                self._claim.reset(context)

    async def configuration(self) -> RecruitmentConfig | None:
        return self.config

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
            if row.encrypted_state is None or row.expires_at <= datetime.now(UTC):
                return ThreadState(
                    thread_id=thread_id,
                    parent_id=row.parent_id,
                    phase="expired",
                    card_id=row.card_id,
                )
            state = ThreadState.model_validate_json(self.fernet.decrypt(row.encrypted_state))
            state.seen_watermark, state.covered_watermark = (
                row.seen_watermark,
                row.covered_watermark,
            )
            if state.revision != row.revision:
                state.revision, state.error, state.card_revision = (
                    row.revision,
                    "refresh_required",
                    "",
                )
            return state

    async def save(
        self,
        state: ThreadState,
        config: RecruitmentConfig,
        *,
        activity: bool = False,
        outbox: Outbox | None = None,
        update_outbox: bool = False,
        processed: tuple[SourceMessage, ...] = (),
    ) -> None:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            claim = self._claim.get()
            if outbox and not update_outbox:
                parent = await session.get(
                    RecruitmentThreadRecord, (self.tenant_id, state.thread_id), with_for_update=True
                )
                if (
                    parent is None
                    or parent.revision != state.revision
                    or parent.seen_watermark != state.covered_watermark
                ):
                    raise ValueError("stale_summary")
            values = dict(
                tenant_id=self.tenant_id,
                thread_id=state.thread_id,
                parent_id=state.parent_id,
                card_id=state.card_id,
                phase=state.phase,
                seen_watermark=state.seen_watermark,
                covered_watermark=state.covered_watermark,
                revision=state.revision,
                encrypted_state=self.seal(state),
                claim_token=claim[1] if claim else None,
                raw_expires_at=now + timedelta(days=config.raw_retention_days),
                expires_at=now + timedelta(days=config.retention_days),
            )
            changes = {
                k: v
                for k, v in values.items()
                if k in {"phase", "revision", "encrypted_state", "card_id", "covered_watermark"}
                or activity
                and k in {"raw_expires_at", "expires_at"}
            }
            statement = insert(RecruitmentThreadRecord).values(**values)
            claim = self._claim.get()
            saved = await session.scalar(
                statement.on_conflict_do_update(
                    index_elements=["tenant_id", "thread_id"],
                    set_=changes,
                    where=RecruitmentThreadRecord.claim_token == claim[1] if claim else None,
                ).returning(RecruitmentThreadRecord.thread_id)
            )
            if saved is None:
                raise RecruitmentBusy("claim_expired")
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
            for message in processed:
                digest = hashlib.sha256(message.model_dump_json().encode()).hexdigest()
                if message.deleted:
                    row = await session.get(
                        RecruitmentMessageRecord, (self.tenant_id, state.thread_id, message.id)
                    )
                    if row and row.deleted and row.observed_at == message.observed_at:
                        digest = row.revision
                await session.execute(
                    update(RecruitmentMessageRecord)
                    .where(
                        RecruitmentMessageRecord.tenant_id == self.tenant_id,
                        RecruitmentMessageRecord.thread_id == state.thread_id,
                        RecruitmentMessageRecord.message_id == message.id,
                        RecruitmentMessageRecord.revision == digest,
                    )
                    .values(processed_revision=digest)
                )

    async def messages(self, thread_id: str, *, pending_only: bool = False) -> list[SourceMessage]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(RecruitmentMessageRecord).where(
                    RecruitmentMessageRecord.tenant_id == self.tenant_id,
                    RecruitmentMessageRecord.thread_id == thread_id,
                    RecruitmentMessageRecord.needs_enrichment.is_(False),
                    *(
                        [
                            RecruitmentMessageRecord.processed_revision.is_distinct_from(
                                RecruitmentMessageRecord.revision
                            )
                        ]
                        if pending_only
                        else []
                    ),
                )
            )
            guild = self.config.guild_id if self.config else "0"
            result: list[SourceMessage] = []
            for row in rows:
                if row.deleted:
                    result.append(
                        SourceMessage(
                            id=row.message_id,
                            content="",
                            deleted=True,
                            observed_at=row.observed_at,
                            url=f"https://discord.com/channels/{guild}/{row.source_channel_id}/{row.message_id}",
                        )
                    )
                elif row.encrypted_source:
                    result.append(
                        SourceMessage.model_validate_json(self.fernet.decrypt(row.encrypted_source))
                    )
            return sorted(result, key=lambda m: int(m.id))

    async def message_manifest(self, thread_id: str) -> list[tuple[str, str, bool]]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(
                    RecruitmentMessageRecord.message_id,
                    RecruitmentMessageRecord.source_channel_id,
                    RecruitmentMessageRecord.deleted,
                ).where(
                    RecruitmentMessageRecord.tenant_id == self.tenant_id,
                    RecruitmentMessageRecord.thread_id == thread_id,
                )
            )
            return [(r[0], r[1], r[2]) for r in rows]

    async def pending_enrichment(self, thread_id: str) -> list[tuple[str, str]]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(
                    RecruitmentMessageRecord.message_id, RecruitmentMessageRecord.source_channel_id
                ).where(
                    RecruitmentMessageRecord.tenant_id == self.tenant_id,
                    RecruitmentMessageRecord.thread_id == thread_id,
                    RecruitmentMessageRecord.needs_enrichment.is_(True),
                )
            )
            return [(r[0], r[1]) for r in rows]

    async def ingest(self, thread_id: str, message: SourceMessage) -> bool:
        async with self.sessions.begin() as session:
            thread = await session.get(
                RecruitmentThreadRecord, (self.tenant_id, thread_id), with_for_update=True
            )
            if not thread or not thread.encrypted_state or thread.expires_at <= datetime.now(UTC):
                raise LookupError("thread_not_enrolled")
            row = await session.get(
                RecruitmentMessageRecord, (self.tenant_id, thread_id, message.id)
            )
            digest = hashlib.sha256(message.model_dump_json().encode()).hexdigest()
            if row:
                if row.deleted or message.observed_at < row.observed_at:
                    return False
                if row.needs_enrichment and row.revision == digest:
                    # A duplicate/partial event may require re-review, but must
                    # not resurrect unchanged erased evidence or renew its TTL.
                    row.needs_enrichment = False
                    return False
                if not row.needs_enrichment and (
                    row.revision == digest
                    or row.encrypted_source is None
                    and message.observed_at <= row.observed_at
                ):
                    return False
            if row is None or not row.needs_enrichment:
                thread.seen_watermark += 1
            claim = self._claim.get()
            if claim and thread.claim_token != claim[1]:
                raise RecruitmentBusy("claim_expired")
            if not claim:
                # A live inbox revision fences a worker that is currently
                # extracting an older snapshot; it must reread, never overwrite.
                thread.claim_token = uuid4().hex
            state = ThreadState.model_validate_json(self.fernet.decrypt(thread.encrypted_state))
            state.revision, state.error, state.card_revision = uuid4().hex, "refresh_required", ""
            thread.revision, thread.encrypted_state = state.revision, self.seal(state)
            config = self.config
            if config is None:
                raise PermissionError("recruitment_unconfigured")
            if not config.enabled:
                raise PermissionError("recruitment_disabled")
            await session.execute(
                insert(RecruitmentMessageRecord)
                .values(
                    tenant_id=self.tenant_id,
                    thread_id=thread_id,
                    message_id=message.id,
                    observed_at=message.observed_at,
                    source_channel_id=(
                        message.url.rstrip("/").split("/")[-2]
                        if "/channels/" in message.url
                        else thread_id
                    ),
                    needs_enrichment=False,
                    deleted=message.deleted,
                    raw_expires_at=datetime.now(UTC) + timedelta(days=config.raw_retention_days),
                    revision=hashlib.sha256(message.model_dump_json().encode()).hexdigest(),
                    encrypted_source=self.seal(message),
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "thread_id", "message_id"],
                    set_={
                        "revision": digest,
                        "encrypted_source": self.seal(message),
                        "observed_at": message.observed_at,
                        "needs_enrichment": False,
                        "deleted": message.deleted,
                        "raw_expires_at": datetime.now(UTC)
                        + timedelta(days=config.raw_retention_days),
                    },
                )
            )
            return True

    async def ignore_bot_source(self, thread_id: str, message_id: str) -> None:
        from sqlalchemy import delete

        async with self.sessions.begin() as session:
            thread = await session.get(
                RecruitmentThreadRecord, (self.tenant_id, thread_id), with_for_update=True
            )
            if not thread:
                return
            await session.execute(
                delete(RecruitmentMessageRecord).where(
                    RecruitmentMessageRecord.tenant_id == self.tenant_id,
                    RecruitmentMessageRecord.thread_id == thread_id,
                    RecruitmentMessageRecord.message_id == message_id,
                    RecruitmentMessageRecord.needs_enrichment.is_(True),
                )
            )
            pending = await session.scalar(
                select(RecruitmentMessageRecord.message_id)
                .where(
                    RecruitmentMessageRecord.tenant_id == self.tenant_id,
                    RecruitmentMessageRecord.thread_id == thread_id,
                    RecruitmentMessageRecord.processed_revision.is_distinct_from(
                        RecruitmentMessageRecord.revision
                    ),
                )
                .limit(1)
            )
            if pending is None:
                thread.covered_watermark = thread.seen_watermark

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
            claim = self._claim.get()
            if claim:
                parent = await session.get(
                    RecruitmentThreadRecord, (self.tenant_id, item.thread_id), with_for_update=True
                )
                if (
                    parent is None
                    or parent.claim_token != claim[1]
                    or parent.expires_at <= datetime.now(UTC)
                ):
                    raise RecruitmentBusy("claim_expired")
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
        await purge_expired(self.sessions, self.tenant_id, now)


async def purge_expired(
    sessions: async_sessionmaker[AsyncSession], tenant_id: UUID, now: datetime
) -> None:
    """Erasure needs neither backend access nor a working decryption key."""
    async with sessions.begin() as session:
        expired = select(RecruitmentThreadRecord.thread_id).where(
            RecruitmentThreadRecord.tenant_id == tenant_id,
            RecruitmentThreadRecord.expires_at <= now,
        )
        await session.execute(
            update(RecruitmentMessageRecord)
            .where(
                RecruitmentMessageRecord.tenant_id == tenant_id,
                RecruitmentMessageRecord.raw_expires_at <= now,
            )
            .values(encrypted_source=None)
        )
        await session.execute(
            update(RecruitmentOutboxRecord)
            .where(
                RecruitmentOutboxRecord.tenant_id == tenant_id,
                RecruitmentOutboxRecord.thread_id.in_(expired),
            )
            .values(encrypted_request=None, state="expired")
        )
        await session.execute(
            update(RecruitmentThreadRecord)
            .where(
                RecruitmentThreadRecord.tenant_id == tenant_id,
                RecruitmentThreadRecord.thread_id.in_(expired),
            )
            .values(encrypted_state=None, phase="expired", claim_token=uuid4().hex)
        )


async def enrolled_tenants(sessions: async_sessionmaker[AsyncSession]) -> list[UUID]:
    """Erasure enumerates retained enrolled tenants, independently of current settings."""
    async with sessions() as session:
        return list(await session.scalars(select(RecruitmentThreadRecord.tenant_id).distinct()))


async def invalidate_source(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    channel_id: str,
    message_id: str,
    *,
    observed_at: datetime | None = None,
    deleted: bool = False,
) -> str | None:
    affected = await invalidate_sources(
        sessions, tenant_id, channel_id, {message_id}, observed_at=observed_at, deleted=deleted
    )
    return next(iter(affected), None)


async def invalidate_sources(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    channel_id: str,
    message_ids: set[str],
    *,
    observed_at: datetime | None = None,
    deleted: bool = False,
) -> set[str]:
    """Accept event metadata and fence approvals before any Discord/model enrichment.

    Parent starters map by (parent_id, thread_id=message_id). Deleted evidence
    stays a content-free tombstone; enrichment failures leave the barrier set.
    """
    from sqlalchemy import and_, or_

    async with sessions.begin() as session:
        threads = await session.scalars(
            select(RecruitmentThreadRecord)
            .where(
                RecruitmentThreadRecord.tenant_id == tenant_id,
                or_(
                    RecruitmentThreadRecord.thread_id == channel_id,
                    and_(
                        RecruitmentThreadRecord.parent_id == channel_id,
                        RecruitmentThreadRecord.thread_id.in_(message_ids),
                    ),
                ),
            )
            .order_by(RecruitmentThreadRecord.thread_id)
            .with_for_update()
        )
        affected: set[str] = set()
        event_deleted = deleted
        for thread in threads:
            for message_id in sorted(message_ids):
                if thread.phase == "expired" or thread.card_id == message_id:
                    continue
                if thread.thread_id != channel_id and thread.thread_id != message_id:
                    continue
                deleted = event_deleted
                affected.add(thread.thread_id)
                old = await session.get(
                    RecruitmentMessageRecord, (tenant_id, thread.thread_id, message_id)
                )
                when = observed_at or (
                    old.observed_at
                    if old and not deleted
                    else datetime.now(UTC)
                    if deleted
                    else datetime.min.replace(tzinfo=UTC)
                )
                if old:
                    when = max(when, old.observed_at)
                    deleted = deleted or old.deleted
                revision = uuid4().hex
                thread.seen_watermark += 1
                thread.claim_token = uuid4().hex
                values = dict(
                    tenant_id=tenant_id,
                    thread_id=thread.thread_id,
                    message_id=message_id,
                    revision=revision if deleted or old is None else old.revision,
                    processed_revision=None,
                    observed_at=when,
                    source_channel_id=channel_id,
                    needs_enrichment=not deleted,
                    deleted=deleted,
                    encrypted_source=old.encrypted_source
                    if old and not deleted and old.raw_expires_at > datetime.now(UTC)
                    else None,
                    raw_expires_at=old.raw_expires_at if old else datetime.now(UTC),
                )
                await session.execute(
                    insert(RecruitmentMessageRecord)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "thread_id", "message_id"],
                        set_={
                            k: v
                            for k, v in values.items()
                            if k not in {"tenant_id", "thread_id", "message_id"}
                        },
                    )
                )
    return affected
