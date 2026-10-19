"""Locked state transitions for durable private routine allocations."""

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import RoutineSession, Tenant
from daimon.core.stores.domain import RoutineLifecycleStatus, RoutineSessionRow, RoutineSessionState
from daimon.core.stores.thread_sessions import mark_dead
from daimon.core.stores.worker_ownership import owner_is_alive
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

TERMINAL = frozenset({"completed", "stopped", "stop_unresolved"})


async def record(
    db: AsyncSession,
    *,
    routine_id: uuid.UUID,
    fire_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    ma_agent_id: str,
    owner_key: int,
    intent_id: uuid.UUID | None = None,
) -> RoutineSessionRow:
    row = RoutineSession(
        id=intent_id or uuid.uuid4(),
        routine_id=routine_id,
        fire_id=fire_id,
        tenant_id=tenant_id,
        account_id=account_id,
        ma_agent_id=ma_agent_id,
        owner_key=owner_key,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return RoutineSessionRow.model_validate(row)


async def get(db: AsyncSession, intent_id: uuid.UUID) -> RoutineSessionRow | None:
    row = await db.get(RoutineSession, intent_id)
    return RoutineSessionRow.model_validate(row) if row is not None else None


async def transition(
    db: AsyncSession,
    intent_id: uuid.UUID,
    state: RoutineSessionState,
    *,
    session_id: str | None = None,
    mapping_id: uuid.UUID | None = None,
    discovered_session_ids: tuple[str, ...] | None = None,
    failure: str | None = None,
) -> RoutineSessionRow:
    row = (
        await db.execute(
            select(RoutineSession)
            .where(RoutineSession.id == intent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if row.state in TERMINAL:
        return RoutineSessionRow.model_validate(row)
    allowed: dict[str, frozenset[str]] = {
        "recorded": frozenset({"recorded", "session_created", "stopping"}),
        "session_created": frozenset({"session_created", "running", "stopping"}),
        "running": frozenset({"running", "completed", "stopping"}),
        "stopping": frozenset({"stopping", "session_created", "stopped", "stop_unresolved"}),
    }
    if state not in allowed[row.state]:
        raise ValueError(f"invalid routine lifecycle transition: {row.state} -> {state}")
    # Late receipts must not move a stop back into execution.
    if row.state != "stopping" or state in {"stopped", "stop_unresolved"}:
        row.state = state
    if session_id is not None:
        row.ma_session_id = session_id
    if mapping_id is not None:
        row.mapping_id = mapping_id
    if discovered_session_ids is not None:
        row.discovered_session_ids = list(discovered_session_ids)
    if failure is not None:
        row.failure_detail = failure[:500]
    row.updated_at = datetime.now(UTC)
    if row.state in TERMINAL:
        row.finished_at = row.updated_at
        row.next_attempt_at = None
        if state != "completed" and row.mapping_id is not None:
            await mark_dead(db, id=row.mapping_id)
    await db.flush()
    return RoutineSessionRow.model_validate(row)


async def begin_attempt(
    db: AsyncSession,
    intent_id: uuid.UUID,
    *,
    owner_key: int,
) -> RoutineSessionRow | None:
    row = (
        await db.execute(
            select(RoutineSession)
            .where(RoutineSession.id == intent_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None or row.state in TERMINAL:
        return None
    if row.owner_key != owner_key and await owner_is_alive(db, row.owner_key):
        return None
    if row.next_attempt_at is not None and row.next_attempt_at > now:
        return None
    row.owner_key = owner_key
    row.state = "stopping"
    row.updated_at = now
    # Reserve the whole attempt before any discovery or provider I/O. A crash
    # consumes this attempt; restarts cannot reset its deadline or its count.
    if row.stop_attempts < 3:
        row.stop_attempts += 1
        row.next_attempt_at = now + timedelta(seconds=30)
    else:
        row.state = "stop_unresolved"
        row.finished_at = now
        row.next_attempt_at = None
        if row.mapping_id is not None:
            await mark_dead(db, id=row.mapping_id)
    await db.flush()
    return RoutineSessionRow.model_validate(row)


async def pending(db: AsyncSession) -> list[RoutineSessionRow]:
    rows = (
        await db.scalars(
            select(RoutineSession).where(
                RoutineSession.state.not_in(TERMINAL),
                (RoutineSession.next_attempt_at.is_(None))
                | (RoutineSession.next_attempt_at <= func.now()),
            )
        )
    ).all()
    return [RoutineSessionRow.model_validate(row) for row in rows]


async def status(
    db: AsyncSession, *, routine_id: uuid.UUID, tenant_id: uuid.UUID
) -> RoutineLifecycleStatus:
    rows = (
        await db.execute(
            select(RoutineSession.state, func.count())
            .where(
                RoutineSession.routine_id == routine_id,
                RoutineSession.tenant_id == tenant_id,
                RoutineSession.state != "completed",
                RoutineSession.state != "stopped",
            )
            .group_by(RoutineSession.state)
        )
    ).all()
    counts = {str(state): int(count) for state, count in rows}
    return RoutineLifecycleStatus(
        pending=sum(count for state, count in counts.items() if state != "stop_unresolved"),
        stop_unresolved=counts.get("stop_unresolved", 0),
    )


async def claim_unresolved_notices(db: AsyncSession, *, platform: str) -> list[RoutineSessionRow]:
    """At-most-once owner notices, independently of the routine result outbox."""
    rows = (
        await db.scalars(
            select(RoutineSession)
            .join(Tenant, Tenant.id == RoutineSession.tenant_id)
            .where(
                Tenant.platform == platform,
                RoutineSession.state == "stop_unresolved",
                RoutineSession.notice_claimed_at.is_(None),
            )
            .with_for_update(of=RoutineSession, skip_locked=True)
            .limit(20)
        )
    ).all()
    for row in rows:
        row.notice_claimed_at = datetime.now(UTC)
    await db.flush()
    return [RoutineSessionRow.model_validate(row) for row in rows]


async def list_for_routine(
    db: AsyncSession,
    *,
    routine_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> list[RoutineSessionRow]:
    rows = (
        await db.scalars(
            select(RoutineSession)
            .where(
                RoutineSession.routine_id == routine_id,
                RoutineSession.tenant_id == tenant_id,
            )
            .order_by(RoutineSession.created_at, RoutineSession.id)
        )
    ).all()
    return [RoutineSessionRow.model_validate(row) for row in rows]
