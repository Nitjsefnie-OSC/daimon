"""Tenant-bound encrypted intake persistence and connection-owned recovery claims."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.fernet import MultiFernet
from daimon.core._models import (
    LeadIntakeConfig,
    LeadIntakeEffect,
    LeadIntakeRecord,
    LeadMailboxState,
    LeadResolutionAudit,
)
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.lead_intake import (
    IntakeBusy,
    IntakeConfig,
    IntakeRecord,
    LeadEffectState,
    LeadSubmission,
)
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, literal, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Effect = LeadEffectState


class MailboxState(BaseModel):
    checkpoint: datetime | None = None
    discovered: list[str] = Field(default_factory=list)
    error_code: str | None = None


class IntakeStore:
    """One tenant per instance; all queries retain the tenant predicate."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], fernet: MultiFernet, tenant_id: UUID
    ) -> None:
        self.sessions, self.fernet, self.tenant_id = sessions, fernet, tenant_id

    def seal(self, value: str) -> bytes:
        return encrypt_token(self.fernet, value)

    def open(self, value: bytes) -> str:
        return decrypt_token(self.fernet, value)

    async def configure(self, config: IntakeConfig, credentials: dict[str, str]) -> None:
        """Operator service entrypoint; credentials never enter tool arguments."""
        values = dict(
            tenant_id=self.tenant_id,
            config=config.model_dump(mode="json"),
            encrypted_credentials=self.seal(json.dumps(credentials)),
        )
        async with self.sessions() as session, session.begin():
            existing = await session.get(LeadIntakeConfig, self.tenant_id, with_for_update=True)
            if existing is not None:
                previous = IntakeConfig.model_validate(existing.config)
                excluded = {"mode", "revision"}
                changed = previous.model_dump(exclude=excluded) != config.model_dump(
                    exclude=excluded
                )
                if config.revision < previous.revision or (
                    changed and config.revision <= previous.revision
                ):
                    raise ValueError("configuration_revision_must_increase")
                if changed:
                    outstanding = await session.scalar(
                        select(LeadIntakeEffect.intake_id)
                        .join(
                            LeadIntakeRecord,
                            (LeadIntakeRecord.tenant_id == LeadIntakeEffect.tenant_id)
                            & (LeadIntakeRecord.id == LeadIntakeEffect.intake_id),
                        )
                        .where(
                            LeadIntakeRecord.tenant_id == self.tenant_id,
                            LeadIntakeRecord.status.in_(("pending", "review")),
                        )
                        .limit(1)
                    )
                    if outstanding is not None:
                        raise ValueError("finish_outstanding_effects_before_reconfiguration")
            await session.execute(
                insert(LeadIntakeConfig)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["tenant_id"],
                    set_={k: v for k, v in values.items() if k != "tenant_id"},
                )
            )

    async def configuration(self) -> tuple[IntakeConfig, dict[str, str]]:
        async with self.sessions() as session:
            row = await session.get(LeadIntakeConfig, self.tenant_id)
            if row is None:
                raise ValueError("intake_not_configured")
            return IntakeConfig.model_validate(row.config), json.loads(
                self.open(row.encrypted_credentials)
            )

    @asynccontextmanager
    async def claim(self, key: str) -> AsyncIterator[None]:
        # A dedicated transaction owns the lock while each intent/result commits
        # independently. Connection death releases it; successors reconcile intents.
        async with self.sessions() as session, session.begin():
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"lead:{self.tenant_id}:{key}"},
            )
            if not acquired:
                raise IntakeBusy("intake_busy")
            yield

    async def accept(
        self, config: IntakeConfig, submission: LeadSubmission, actor_id: str | None
    ) -> IntakeRecord:
        digest = hashlib.sha256(
            f"{self.tenant_id}:{config.source_connection_id}:{submission.source.key}".encode()
        ).hexdigest()
        now = datetime.now(UTC)
        raw = submission.evidence.raw_source
        evidence = submission.evidence.model_copy(update={"raw_source": None})
        submission = submission.model_copy(update={"evidence": evidence})
        record = IntakeRecord(
            id=uuid4(),
            tenant_id=self.tenant_id,
            source_digest=digest,
            status="rejected" if submission.classification == "other" else "pending",
            submission=submission,
            actor_id=actor_id,
            config_revision=config.revision,
            created_at=now,
            expires_at=now + timedelta(days=config.retention_days),
            raw_expires_at=now + timedelta(days=config.raw_retention_days),
        )
        async with self.sessions() as session, session.begin():
            await session.execute(
                insert(LeadIntakeRecord)
                .values(
                    tenant_id=self.tenant_id,
                    id=record.id,
                    source_digest=digest,
                    actor_digest=actor_digest(self.tenant_id, actor_id) if actor_id else None,
                    status=record.status,
                    encrypted_payload=self.seal(record.model_dump_json()),
                    encrypted_raw=self.seal(raw) if raw else None,
                    created_at=now,
                    expires_at=record.expires_at,
                    raw_expires_at=record.raw_expires_at,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "source_digest"])
            )
            row = (
                await session.execute(
                    select(LeadIntakeRecord).where(
                        LeadIntakeRecord.tenant_id == self.tenant_id,
                        LeadIntakeRecord.source_digest == digest,
                    )
                )
            ).scalar_one()
            return self._record(row)

    def _record(self, row: LeadIntakeRecord) -> IntakeRecord:
        if row.encrypted_payload is not None:
            return IntakeRecord.model_validate_json(self.open(row.encrypted_payload))
        return IntakeRecord(
            id=row.id,
            tenant_id=self.tenant_id,
            source_digest=row.source_digest,
            status="expired",
            submission=None,
            config_revision=0,
            created_at=row.created_at,
            expires_at=row.expires_at,
            raw_expires_at=row.raw_expires_at,
        )

    async def seen_source(self, config: IntakeConfig, message_id: str) -> bool:
        key = f"gmail:{config.gmail_mailbox.strip().lower()}:{message_id}"
        digest = hashlib.sha256(
            f"{self.tenant_id}:{config.source_connection_id}:{key}".encode()
        ).hexdigest()
        async with self.sessions() as session:
            return (
                await session.scalar(
                    select(LeadIntakeRecord.id).where(
                        LeadIntakeRecord.tenant_id == self.tenant_id,
                        LeadIntakeRecord.source_digest == digest,
                    )
                )
            ) is not None

    async def get(self, intake_id: UUID) -> IntakeRecord:
        async with self.sessions() as session:
            row = await session.get(LeadIntakeRecord, (self.tenant_id, intake_id))
            if row is None:
                raise ValueError("intake_not_found")
            record = self._record(row)
            effects = (
                await session.execute(
                    select(LeadIntakeEffect)
                    .where(
                        LeadIntakeEffect.tenant_id == self.tenant_id,
                        LeadIntakeEffect.intake_id == intake_id,
                    )
                    .order_by(LeadIntakeEffect.created_at)
                )
            ).scalars()
            record.effects = (
                [
                    LeadEffectState(
                        step=e.step,
                        stable_key=e.stable_key,
                        state=e.state,
                        remote_id=e.remote_id,
                        created_at=e.created_at,
                    )
                    for e in effects
                ]
                if record.submission
                else []
            )
            return record

    async def list(
        self, limit: int = 100, *, pending_only: bool = False, after_id: UUID | None = None
    ) -> list[IntakeRecord]:
        async with self.sessions() as session:
            query = select(LeadIntakeRecord).where(LeadIntakeRecord.tenant_id == self.tenant_id)
            if pending_only:
                query = query.where(LeadIntakeRecord.status.in_(("pending", "review")))
            if after_id is not None:
                after = await self.get(after_id)
                query = query.where(
                    tuple_(LeadIntakeRecord.created_at, LeadIntakeRecord.id)
                    > tuple_(literal(after.created_at), literal(after.id))
                )
            rows = (
                await session.execute(
                    query.order_by(LeadIntakeRecord.created_at, LeadIntakeRecord.id).limit(
                        min(limit, 100)
                    )
                )
            ).scalars()
            return [self._record(row) for row in rows]

    async def health(self) -> dict[str, object]:
        async with self.sessions() as session:
            counts = await session.execute(
                select(LeadIntakeRecord.status, func.count())
                .where(LeadIntakeRecord.tenant_id == self.tenant_id)
                .group_by(LeadIntakeRecord.status)
            )
            oldest = await session.scalar(
                select(func.min(LeadIntakeRecord.created_at)).where(
                    LeadIntakeRecord.tenant_id == self.tenant_id,
                    LeadIntakeRecord.status.in_(("pending", "review")),
                )
            )
            return {"status_counts": dict(counts.tuples().all()), "oldest_pending_at": oldest}

    async def save(self, record: IntakeRecord) -> None:
        if record.tenant_id != self.tenant_id:
            raise ValueError("tenant_mismatch")
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(LeadIntakeRecord)
                .where(
                    LeadIntakeRecord.tenant_id == self.tenant_id,
                    LeadIntakeRecord.id == record.id,
                    LeadIntakeRecord.encrypted_payload.is_not(None),
                )
                .values(status=record.status, encrypted_payload=self.seal(record.model_dump_json()))
            )

    async def effect(self, intake_id: UUID, step: str) -> Effect | None:
        async with self.sessions() as session:
            row = await session.get(LeadIntakeEffect, (self.tenant_id, intake_id, step))
            return (
                Effect(
                    step=row.step,
                    stable_key=row.stable_key,
                    state=row.state,
                    remote_id=row.remote_id,
                    created_at=row.created_at,
                )
                if row
                else None
            )

    async def intent(self, intake_id: UUID, step: str, stable_key: str) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                insert(LeadIntakeEffect)
                .values(
                    tenant_id=self.tenant_id,
                    intake_id=intake_id,
                    step=step,
                    stable_key=stable_key,
                    state="uncertain",
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing()
            )

    async def finish(self, intake_id: UUID, step: str, remote_id: str) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(LeadIntakeEffect)
                .where(
                    LeadIntakeEffect.tenant_id == self.tenant_id,
                    LeadIntakeEffect.intake_id == intake_id,
                    LeadIntakeEffect.step == step,
                )
                .values(state="completed", remote_id=remote_id)
            )

    async def audit(self, record: IntakeRecord, actor_id: str, before: str, after: str) -> None:
        if record.tenant_id != self.tenant_id:
            raise ValueError("tenant_mismatch")
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(LeadIntakeRecord)
                .where(
                    LeadIntakeRecord.tenant_id == self.tenant_id,
                    LeadIntakeRecord.id == record.id,
                    LeadIntakeRecord.encrypted_payload.is_not(None),
                )
                .values(status=record.status, encrypted_payload=self.seal(record.model_dump_json()))
            )
            session.add(
                LeadResolutionAudit(
                    id=uuid4(),
                    tenant_id=self.tenant_id,
                    intake_id=record.id,
                    encrypted_resolution=self.seal(
                        json.dumps(dict(actor=actor_id, before=before, after=after))
                    ),
                    created_at=datetime.now(UTC),
                )
            )

    async def mailbox(self, connection: str) -> MailboxState:
        async with self.sessions() as session:
            row = await session.get(LeadMailboxState, (self.tenant_id, connection))
            return (
                MailboxState.model_validate_json(self.open(row.encrypted_state))
                if row
                else MailboxState()
            )

    async def save_mailbox(self, connection: str, state: MailboxState) -> None:
        async with self.sessions() as session, session.begin():
            encrypted = self.seal(state.model_dump_json())
            await session.execute(
                insert(LeadMailboxState)
                .values(
                    tenant_id=self.tenant_id, connection_id=connection, encrypted_state=encrypted
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "connection_id"],
                    set_={"encrypted_state": encrypted},
                )
            )

    async def purge(self, now: datetime) -> None:
        """Erase all lead content/effects/audit, keeping only the source tombstone."""
        async with self.sessions() as session, session.begin():
            expired = select(LeadIntakeRecord.id).where(
                LeadIntakeRecord.tenant_id == self.tenant_id, LeadIntakeRecord.expires_at <= now
            )
            for model in (LeadIntakeEffect, LeadResolutionAudit):
                await session.execute(
                    delete(model).where(
                        model.tenant_id == self.tenant_id, model.intake_id.in_(expired)
                    )
                )
            await session.execute(
                update(LeadIntakeRecord)
                .where(
                    LeadIntakeRecord.tenant_id == self.tenant_id,
                    LeadIntakeRecord.raw_expires_at <= now,
                )
                .values(encrypted_raw=None)
            )
            await session.execute(
                update(LeadIntakeRecord)
                .where(
                    LeadIntakeRecord.tenant_id == self.tenant_id, LeadIntakeRecord.expires_at <= now
                )
                .values(
                    encrypted_payload=None, encrypted_raw=None, status="expired", actor_digest=None
                )
            )


@asynccontextmanager
async def routine_execution(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    routine_id: UUID,
    *,
    register: bool = True,
) -> AsyncIterator[None]:
    """A live, expiring grant ends with the scheduler fire, including cancellation."""
    from daimon.core._models import LeadIntakeExecution
    from daimon.core.lead_intake_execution import current_intake_execution

    identity = uuid4()
    if register:
        async with sessions() as session, session.begin():
            session.add(
                LeadIntakeExecution(
                    tenant_id=tenant_id,
                    id=identity,
                    routine_id=routine_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
    token = current_intake_execution.set((routine_id, identity))
    try:
        yield
    finally:
        current_intake_execution.reset(token)
        if register:
            async with sessions() as session, session.begin():
                await session.execute(
                    delete(LeadIntakeExecution).where(
                        LeadIntakeExecution.tenant_id == tenant_id,
                        LeadIntakeExecution.id == identity,
                    )
                )


async def execution_is_active(
    session: AsyncSession, tenant_id: UUID, routine_id: UUID, execution_id: UUID
) -> bool:
    from daimon.core._models import LeadIntakeExecution

    row = await session.get(LeadIntakeExecution, (tenant_id, execution_id))
    return row is not None and row.routine_id == routine_id and row.expires_at > datetime.now(UTC)


async def purge_expired(session: AsyncSession, now: datetime) -> None:
    """Scheduler maintenance uses only ciphertext expiry; no decryption key needed."""
    expired = select(LeadIntakeRecord.tenant_id, LeadIntakeRecord.id).where(
        LeadIntakeRecord.expires_at <= now
    )
    for model in (LeadIntakeEffect, LeadResolutionAudit):
        await session.execute(
            delete(model).where(tuple_(model.tenant_id, model.intake_id).in_(expired))
        )
    await session.execute(
        update(LeadIntakeRecord)
        .where(LeadIntakeRecord.raw_expires_at <= now)
        .values(encrypted_raw=None)
    )
    await session.execute(
        update(LeadIntakeRecord)
        .where(LeadIntakeRecord.expires_at <= now)
        .values(encrypted_payload=None, encrypted_raw=None, status="expired", actor_digest=None)
    )


def actor_digest(tenant_id: UUID, actor_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{actor_id}".encode()).hexdigest()


async def erase_actor(session: AsyncSession, tenant_id: UUID, actor_id: str) -> int:
    """Privacy erasure removes actor-submitted evidence and its effects/audit."""
    ids = select(LeadIntakeRecord.id).where(
        LeadIntakeRecord.tenant_id == tenant_id,
        LeadIntakeRecord.actor_digest == actor_digest(tenant_id, actor_id),
        LeadIntakeRecord.encrypted_payload.is_not(None),
    )
    identities = list(await session.scalars(ids))
    for model in (LeadIntakeEffect, LeadResolutionAudit):
        await session.execute(
            delete(model).where(model.tenant_id == tenant_id, model.intake_id.in_(identities))
        )
    await session.execute(
        update(LeadIntakeRecord)
        .where(LeadIntakeRecord.tenant_id == tenant_id, LeadIntakeRecord.id.in_(identities))
        .values(encrypted_payload=None, encrypted_raw=None, actor_digest=None, status="expired")
    )
    return len(identities)


async def count_actor(session: AsyncSession, tenant_id: UUID, actor_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(LeadIntakeRecord)
            .where(
                LeadIntakeRecord.tenant_id == tenant_id,
                LeadIntakeRecord.actor_digest == actor_digest(tenant_id, actor_id),
                LeadIntakeRecord.encrypted_payload.is_not(None),
            )
        )
        or 0
    )


async def clear_effect(store: IntakeStore, intake_id: UUID, step: str) -> None:
    """Called only after authorized provider absence reconciliation under source claim."""
    async with store.sessions() as session, session.begin():
        await session.execute(
            delete(LeadIntakeEffect).where(
                LeadIntakeEffect.tenant_id == store.tenant_id,
                LeadIntakeEffect.intake_id == intake_id,
                LeadIntakeEffect.step == step,
            )
        )


async def configured_routines(
    session: AsyncSession, tenant_ids: set[UUID]
) -> set[tuple[UUID, UUID]]:
    rows = await session.execute(
        select(LeadIntakeConfig.tenant_id, LeadIntakeConfig.config).where(
            LeadIntakeConfig.tenant_id.in_(tenant_ids)
        )
    )
    return {
        (tenant, config.routine_id)
        for tenant, payload in rows
        if (config := IntakeConfig.model_validate(payload)).routine_id is not None
    }
