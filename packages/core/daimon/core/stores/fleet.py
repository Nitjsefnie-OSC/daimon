"""Fleet runs and workers: durable rows behind `daimon.core.fleet`.

Every write is one conditional UPDATE: the row's `version` must match, and its
status must be one the target may follow (`WORKER_TRANSITIONS` /
`RUN_TRANSITIONS`). A lost race or a refused move raises `FleetConflict` and
changes nothing. The tenant concurrency cap is counted from these rows under a
per-tenant advisory lock, so it survives restarts and can never drift.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import FleetRun as FleetRunRow
from daimon.core._models import FleetWorker as FleetWorkerRow
from daimon.core.fleet import (
    RUN_TRANSITIONS,
    TERMINAL,
    WORKER_TRANSITIONS,
    FleetConflict,
    FleetRun,
    FleetWorker,
    ResultPointer,
    RunStatus,
    WorkerStatus,
    worker_slot,
)
from daimon.core.stores import mux_state
from pydantic import JsonValue
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ACTIVE: tuple[WorkerStatus, ...] = ("starting", "running")


def _run(row: FleetRunRow) -> FleetRun:
    return FleetRun(
        id=row.id,
        tenant_id=row.tenant_id,
        orchestrator_account_id=row.orchestrator_account_id,
        orchestrator_session_id=row.orchestrator_session_id,
        orchestrator_binding_id=row.orchestrator_binding_id,
        platform=row.platform,
        channel_id=row.channel_id,
        thread_id=row.thread_id,
        status=cast(RunStatus, row.status),
        total_budget_usd=row.total_budget_usd,
        reserved_usd=row.reserved_usd,
        observed_usd=row.observed_usd,
        deadline_at=row.deadline_at,
        max_workers=row.max_workers,
        quorum=row.quorum,
        version=row.version,
    )


def _worker(row: FleetWorkerRow) -> FleetWorker:
    result = (
        ResultPointer(
            session_id=row.result_session_id,
            sequence=row.result_sequence,
            digest=row.result_digest,
            excerpt=row.result_excerpt or "",
        )
        if row.result_session_id is not None
        and row.result_sequence is not None
        and row.result_digest is not None
        else None
    )
    return FleetWorker(
        id=row.id,
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        ordinal=row.ordinal,
        role=row.role,
        spec=row.spec,
        spec_digest=row.spec_digest,
        backend=row.backend,
        profile=row.profile,
        model=row.model,
        binding_id=row.binding_id,
        status=cast(WorkerStatus, row.status),
        budget_usd=row.budget_usd,
        reserved_usd=row.reserved_usd,
        deadline_at=row.deadline_at,
        max_turns=row.max_turns,
        turns_used=row.turns_used,
        attempts=row.attempts,
        reviewer_of=row.reviewer_of,
        result_contract=row.result_contract,
        result=result,
        error_code=row.error_code,
        version=row.version,
    )


def _worker_from(target: WorkerStatus) -> list[str]:
    return [status for status, nexts in WORKER_TRANSITIONS.items() if target in nexts]


def _run_from(target: RunStatus) -> list[str]:
    return [status for status, nexts in RUN_TRANSITIONS.items() if target in nexts]


async def create_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    orchestrator_account_id: uuid.UUID,
    orchestrator_session_id: str,
    orchestrator_binding_id: str,
    platform: str,
    channel_id: str,
    thread_id: str,
    operation_key: str,
    total_budget_usd: Decimal,
    deadline_at: datetime,
    max_workers: int,
    created_by: str,
    quorum: int | None = None,
) -> FleetRun:
    """A new running fleet run, or the one this tenant already made under the key."""
    async with sessionmaker.begin() as session:
        await session.execute(
            insert(FleetRunRow)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                orchestrator_account_id=orchestrator_account_id,
                orchestrator_session_id=orchestrator_session_id,
                orchestrator_binding_id=orchestrator_binding_id,
                platform=platform,
                channel_id=channel_id,
                thread_id=thread_id,
                operation_key=operation_key,
                status="running",
                total_budget_usd=total_budget_usd,
                deadline_at=deadline_at,
                max_workers=max_workers,
                quorum=quorum,
                created_by=created_by,
            )
            .on_conflict_do_nothing(constraint="uq_fleet_run_operation")
        )
        row = (
            await session.execute(
                select(FleetRunRow).where(
                    FleetRunRow.tenant_id == tenant_id, FleetRunRow.operation_key == operation_key
                )
            )
        ).scalar_one()
        return _run(row)


async def add_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    ordinal: int,
    role: str,
    spec: Mapping[str, JsonValue],
    spec_digest: str,
    backend: str,
    profile: str,
    model: str,
    budget_usd: Decimal,
    deadline_at: datetime,
    max_turns: int,
    reviewer_of: uuid.UUID | None = None,
    result_contract: Mapping[str, JsonValue] | None = None,
) -> FleetWorker:
    """A pending worker at `ordinal`, or the same one again; another spec there is a conflict.

    The run must be running, the ordinal below its `max_workers`, and the worker's
    deadline no later than the run's.
    """
    async with sessionmaker.begin() as session:
        run = (
            await session.execute(
                select(FleetRunRow)
                .where(FleetRunRow.tenant_id == tenant_id, FleetRunRow.id == run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise FleetConflict(f"no fleet run {run_id} in this tenant")
        existing = (
            await session.execute(
                select(FleetWorkerRow).where(
                    FleetWorkerRow.run_id == run_id, FleetWorkerRow.ordinal == ordinal
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.spec_digest != spec_digest:
                raise FleetConflict(f"worker {ordinal} of run {run_id} has another spec")
            return _worker(existing)
        if run.status != "running":
            raise FleetConflict(f"run {run_id} is {run.status}")
        if not 0 <= ordinal < run.max_workers:
            raise FleetConflict(f"run {run_id} allows {run.max_workers} workers")
        if deadline_at > run.deadline_at:
            raise FleetConflict("a worker's deadline cannot outlast its run")
        row = FleetWorkerRow(
            id=uuid.uuid4(),
            run_id=run_id,
            tenant_id=tenant_id,
            ordinal=ordinal,
            role=role,
            spec=dict(spec),
            spec_digest=spec_digest,
            backend=backend,
            profile=profile,
            model=model,
            status="pending",
            budget_usd=budget_usd,
            deadline_at=deadline_at,
            max_turns=max_turns,
            reviewer_of=reviewer_of,
            result_contract=dict(result_contract) if result_contract is not None else None,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return _worker(row)


async def claim_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    cap: int,
) -> FleetWorker | None:
    """The run's lowest pending worker, now `starting`, if the tenant has room.

    The tenant's starting and running workers are counted under a per-tenant
    advisory lock, so concurrent claims (any process) never exceed `cap`.
    """
    async with sessionmaker.begin() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"fleet:{tenant_id}"},
        )
        active = (
            await session.execute(
                select(func.count())
                .select_from(FleetWorkerRow)
                .where(FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.status.in_(_ACTIVE))
            )
        ).scalar_one()
        if active >= cap:
            return None
        row = (
            await session.execute(
                update(FleetWorkerRow)
                .where(
                    FleetWorkerRow.id
                    == select(FleetWorkerRow.id)
                    .join(
                        FleetRunRow,
                        (FleetRunRow.id == FleetWorkerRow.run_id)
                        & (FleetRunRow.tenant_id == FleetWorkerRow.tenant_id),
                    )
                    .where(
                        FleetWorkerRow.tenant_id == tenant_id,
                        FleetWorkerRow.run_id == run_id,
                        FleetWorkerRow.status == "pending",
                        FleetRunRow.status == "running",
                    )
                    .order_by(FleetWorkerRow.ordinal)
                    .limit(1)
                    .with_for_update(of=FleetWorkerRow, skip_locked=True)
                    .scalar_subquery()
                )
                .values(
                    status="starting",
                    started_at=func.now(),
                    updated_at=func.now(),
                    version=FleetWorkerRow.version + 1,
                )
                .returning(FleetWorkerRow)
            )
        ).scalar_one_or_none()
        return _worker(row) if row is not None else None


async def _move_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    target: WorkerStatus,
    values: Mapping[str, Any],
) -> FleetWorker:
    async with sessionmaker.begin() as session:
        row = (
            await session.execute(
                update(FleetWorkerRow)
                .where(
                    FleetWorkerRow.tenant_id == tenant_id,
                    FleetWorkerRow.id == worker_id,
                    FleetWorkerRow.version == expected_version,
                    FleetWorkerRow.status.in_(_worker_from(target)),
                )
                .values(
                    status=target,
                    version=FleetWorkerRow.version + 1,
                    updated_at=func.now(),
                    **values,
                )
                .returning(FleetWorkerRow)
            )
        ).scalar_one_or_none()
    if row is None:
        raise FleetConflict(
            f"worker {worker_id} cannot become {target} at version {expected_version}"
        )
    return _worker(row)


async def mark_running(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    binding_id: str,
) -> FleetWorker:
    """`starting -> running` once the worker's session is bound."""
    return await _move_worker(
        sessionmaker,
        tenant_id=tenant_id,
        worker_id=worker_id,
        expected_version=expected_version,
        target="running",
        values={"binding_id": binding_id},
    )


async def record_result(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    result: ResultPointer,
) -> FleetWorker:
    """`running -> succeeded`, pointing at the result already in the worker's journal."""
    return await _move_worker(
        sessionmaker,
        tenant_id=tenant_id,
        worker_id=worker_id,
        expected_version=expected_version,
        target="succeeded",
        values={
            "result_session_id": result.session_id,
            "result_sequence": result.sequence,
            "result_digest": result.digest,
            "result_excerpt": result.excerpt,
            "finished_at": func.now(),
        },
    )


async def finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    status: WorkerStatus,
    error_code: str,
) -> FleetWorker:
    """End a worker without a result: failed, cancelled, timed out or over budget."""
    if status not in TERMINAL or status == "succeeded":
        raise FleetConflict(f"{status} is not an unsuccessful end")
    return await _move_worker(
        sessionmaker,
        tenant_id=tenant_id,
        worker_id=worker_id,
        expected_version=expected_version,
        target=status,
        values={"error_code": error_code, "finished_at": func.now()},
    )


async def requeue(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    worker: FleetWorker,
    max_attempts: int,
) -> FleetWorker:
    """Recovery for a worker lost before its turn was sent: back to `pending`.

    Past `max_attempts` it fails instead (`worker_lost`). The caller decides it was
    never sent (its spawn operation never claimed a send); a sent turn is
    reconciled, never requeued.
    """
    if worker.attempts + 1 >= max_attempts:
        return await finish(
            sessionmaker,
            tenant_id=tenant_id,
            worker_id=worker.id,
            expected_version=worker.version,
            status="failed",
            error_code="worker_lost",
        )
    return await _move_worker(
        sessionmaker,
        tenant_id=tenant_id,
        worker_id=worker.id,
        expected_version=worker.version,
        target="pending",
        values={"attempts": FleetWorkerRow.attempts + 1, "started_at": None},
    )


async def reserve(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    worker_id: uuid.UUID,
    expected_version: int,
    usd: Decimal,
) -> FleetWorker:
    """Set a worker's reservation; its run's total moves with it, within the run's budget."""
    async with sessionmaker.begin() as session:
        row = (
            await session.execute(
                select(FleetWorkerRow)
                .where(FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.id == worker_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.version != expected_version or row.status in TERMINAL:
            raise FleetConflict(f"worker {worker_id} cannot reserve at version {expected_version}")
        if not Decimal(0) <= usd <= row.budget_usd:
            raise FleetConflict("a reservation must stay within the worker's budget")
        delta = usd - row.reserved_usd
        run = (
            await session.execute(
                update(FleetRunRow)
                .where(
                    FleetRunRow.tenant_id == tenant_id,
                    FleetRunRow.id == row.run_id,
                    FleetRunRow.reserved_usd + delta <= FleetRunRow.total_budget_usd,
                )
                .values(reserved_usd=FleetRunRow.reserved_usd + delta, updated_at=func.now())
                .returning(FleetRunRow.id)
            )
        ).scalar_one_or_none()
        if run is None:
            raise FleetConflict("the reservation would exceed the run's total budget")
        row.reserved_usd = usd
        row.version += 1
        await session.flush()
        await session.refresh(row)
        return _worker(row)


async def set_observed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    usd: Decimal,
) -> FleetRun:
    """Record the run's settled spend so far, as the budget watchdog last read it."""
    async with sessionmaker.begin() as session:
        row = (
            await session.execute(
                update(FleetRunRow)
                .where(FleetRunRow.tenant_id == tenant_id, FleetRunRow.id == run_id)
                .values(observed_usd=usd, updated_at=func.now())
                .returning(FleetRunRow)
            )
        ).scalar_one_or_none()
    if row is None:
        raise FleetConflict(f"no fleet run {run_id} in this tenant")
    return _run(row)


async def set_run_status(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    expected_version: int,
    status: RunStatus,
) -> FleetRun:
    finished = {"finished_at": func.now()} if status not in RUN_TRANSITIONS else {}
    async with sessionmaker.begin() as session:
        row = (
            await session.execute(
                update(FleetRunRow)
                .where(
                    FleetRunRow.tenant_id == tenant_id,
                    FleetRunRow.id == run_id,
                    FleetRunRow.version == expected_version,
                    FleetRunRow.status.in_(_run_from(status)),
                )
                .values(
                    status=status,
                    version=FleetRunRow.version + 1,
                    updated_at=func.now(),
                    **finished,
                )
                .returning(FleetRunRow)
            )
        ).scalar_one_or_none()
    if row is None:
        raise FleetConflict(f"run {run_id} cannot become {status} at version {expected_version}")
    return _run(row)


async def run_status(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[FleetRun, tuple[FleetWorker, ...]]:
    async with sessionmaker() as session:
        run = (
            await session.execute(
                select(FleetRunRow).where(
                    FleetRunRow.tenant_id == tenant_id, FleetRunRow.id == run_id
                )
            )
        ).scalar_one_or_none()
        if run is None:
            raise FleetConflict(f"no fleet run {run_id} in this tenant")
        workers = (
            await session.execute(
                select(FleetWorkerRow)
                .where(FleetWorkerRow.tenant_id == tenant_id, FleetWorkerRow.run_id == run_id)
                .order_by(FleetWorkerRow.ordinal)
            )
        ).scalars()
        return _run(run), tuple(_worker(worker) for worker in workers)


async def stale_workers(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime, grace: timedelta
) -> Sequence[tuple[FleetRun, FleetWorker]]:
    """Starting or running workers the recovery sweep must look at.

    A worker is stale once its deadline has passed, or when it has been active
    for longer than `grace` and its slot holds no unexpired lease (its process is
    gone). `grace` covers the gap between a claim and the driver's first lease.
    """
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(FleetRunRow, FleetWorkerRow)
                .join(
                    FleetWorkerRow,
                    (FleetWorkerRow.run_id == FleetRunRow.id)
                    & (FleetWorkerRow.tenant_id == FleetRunRow.tenant_id),
                )
                .where(FleetWorkerRow.status.in_(_ACTIVE))
                .order_by(FleetWorkerRow.started_at)
            )
        ).all()
        stale: list[tuple[FleetRun, FleetWorker]] = []
        for run_row, worker_row in rows:
            run, worker = _run(run_row), _worker(worker_row)
            if worker.deadline_at <= now:
                stale.append((run, worker))
                continue
            started = worker_row.started_at
            if started is not None and now - started <= grace:
                continue
            lease = await mux_state.active_lease(session, worker_slot(run, worker))
            if lease is None or lease.expires_at <= now:
                stale.append((run, worker))
        return stale
