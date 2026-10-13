"""Completion writers are closed; future-row fixtures prove reader/lease behavior only."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from daimon.core._models import FleetWorker as FleetWorkerRow
from daimon.core.continuity.continuation import (
    ContinuationDecision,
    ContinuationReasonUnavailable,
    ContinuationRequest,
    record_continuation,
)
from daimon.core.continuity.dispatch import WorkerFinishedDecision, dispatch_pending_continuations
from daimon.core.continuity.wakes import (
    WAKE_RETRY_DELAY,
    WAKE_RUN_LEASE,
    FrozenWorkerBatch,
    abandon_interrupted_wakes,
    arm_worker_finished,
    claim_wake,
    enqueue_wake,
    enqueue_worker_finished,
    list_dispatchable_wakes,
    settle_wake,
    start_wake,
    worker_finished_key,
)
from daimon.core.fleet import FleetRun, FleetWorker
from daimon.core.stores import fleet
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.task_continuations import record_continuation as insert_reader_fixture
from daimon.testing.db import build_test_engine, isolated_test_schema, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_NOW = datetime(2026, 10, 12, 18, tzinfo=UTC)


@pytest.fixture
def pooled(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    require_owned_sessionmaker(factory)
    return factory


async def _run(
    sm: async_sessionmaker[AsyncSession],
    *,
    quorum: int | None = None,
    deadline_at: datetime | None = None,
    max_workers: int = 2,
) -> FleetRun:
    async with sm.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    return await fleet.create_run(
        sm,
        tenant_id=tenant.id,
        orchestrator_account_id=account.id,
        orchestrator_session_id="orchestrator-session",
        orchestrator_binding_id="orchestrator-binding",
        platform="discord",
        channel_id="channel",
        thread_id="thread",
        operation_key="spawn",
        total_budget_usd=Decimal(4),
        deadline_at=deadline_at or _NOW + timedelta(hours=1),
        max_workers=max_workers,
        created_by="authorized-host",
        quorum=quorum,
    )


async def _worker(
    sm: async_sessionmaker[AsyncSession],
    run: FleetRun,
    ordinal: int,
    reviewer_of: uuid.UUID | None = None,
) -> FleetWorker:
    return await fleet.add_worker(
        sm,
        tenant_id=run.tenant_id,
        run_id=run.id,
        ordinal=ordinal,
        role="research",
        spec={},
        spec_digest=f"spec-{ordinal}",
        backend="openai",
        profile="openai.persistent_workspace",
        model="gpt-6-luna",
        budget_usd=Decimal(2),
        deadline_at=run.deadline_at,
        max_turns=1,
        reviewer_of=reviewer_of,
    )


def _request(run: FleetRun) -> ContinuationRequest:
    return ContinuationRequest(
        tenant_id=run.tenant_id,
        requester_account_id=run.orchestrator_account_id,
        requester_external_user_id="human",
        platform="discord",
        parent_channel_id=run.channel_id,
        thread_id=run.thread_id,
        target_ma_agent_id="orchestrator-agent",
        target_name="orchestrator",
        reason="worker_finished",
        requested_work="Collect the completed workers and verify their result contracts.",
        idempotency_key=worker_finished_key(run.id),
    )


async def _seed_reader_fixture(sm: async_sessionmaker[AsyncSession], run: FleetRun) -> None:
    # Deliberate future-row fixture, never a successful host producer proof.
    async with sm.begin() as session:
        await insert_reader_fixture(session, **_request(run).model_dump(), available_at=_NOW)


async def _read(sm: async_sessionmaker[AsyncSession], run: FleetRun) -> TaskContinuationRow | None:
    async with sm() as session:
        return await get_continuation(session, idempotency_key=worker_finished_key(run.id))


@pytest.mark.parametrize("entrypoint", ["worker", "wake", "continuation"])
async def test_completion_producers_refuse_before_any_database_access(entrypoint: str) -> None:
    class ConnectionTrap:
        def in_transaction(self):
            return True

        def begin(self):
            raise AssertionError("Refusal must precede any transaction/connection")

        async def execute(self, *args, **kwargs):
            raise AssertionError("Refusal must precede any query")

    run_id = uuid.uuid4()
    request = ContinuationRequest(
        tenant_id=uuid.uuid4(),
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        requester_account_id=uuid.uuid4(),
        requester_external_user_id="human",
        target_ma_agent_id="agent",
        target_name="orchestrator",
        reason="worker_finished",
        idempotency_key=worker_finished_key(run_id),
        requested_work="Collect the workers.",
    )
    trap = ConnectionTrap()
    if entrypoint == "worker":
        assert not await enqueue_worker_finished(trap, request, run_id=run_id, now=_NOW)
    else:
        with pytest.raises(ContinuationReasonUnavailable, match="compatible-reader release"):
            if entrypoint == "wake":
                await enqueue_wake(trap, request, available_at=_NOW)
            else:
                await record_continuation(trap, request)


@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("terminal", ["cancelled", "timed_out"])
async def test_readiness_at_deadline_preserves_terminal_transition_without_wake(
    pooled: async_sessionmaker[AsyncSession],
    offset: int,
    terminal: str,
) -> None:
    run = await _run(pooled, deadline_at=datetime.now(UTC) + timedelta(hours=1))
    worker = await _worker(pooled, run, 0)
    if terminal == "timed_out":
        worker = await fleet.claim_worker(pooled, tenant_id=run.tenant_id, run_id=run.id, cap=1)
        assert worker is not None and worker.status == "starting"
    async with pooled.begin() as session:
        await fleet.run_status(session, tenant_id=run.tenant_id, run_id=run.id, for_update=True)
        finished = await fleet.finish(
            session,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=worker.version,
            status=terminal,
            error_code=terminal,
        )
        assert not await enqueue_worker_finished(
            session,
            _request(run),
            run_id=run.id,
            now=run.deadline_at + timedelta(seconds=offset),
        )
    current, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and current.status == "running" and current.version == run.version
    assert workers == (finished,) and workers[0].status == terminal
    assert workers[0].version == worker.version + 1 and workers[0].error_code == terminal
    async with pooled() as session:
        finished_at = await session.scalar(
            select(FleetWorkerRow.finished_at).where(FleetWorkerRow.id == worker.id)
        )
    assert finished_at is not None
    assert await _read(pooled, run) is None


async def test_inactive_transaction_still_refuses_without_autobegin(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    async with pooled() as session:
        with pytest.raises(ValueError, match="active transaction"):
            await enqueue_worker_finished(session, _request(run), run_id=run.id, now=_NOW)
        assert not session.in_transaction()
        async with session.begin():
            with pytest.raises(ValueError, match="stable wake identity"):
                await enqueue_worker_finished(
                    session,
                    _request(run).model_copy(update={"idempotency_key": uuid.uuid4()}),
                    run_id=run.id,
                    now=_NOW,
                )
    assert await _read(pooled, run) is None


async def _dispatch(
    sm: async_sessionmaker[AsyncSession],
    run: FleetRun,
    decision: WorkerFinishedDecision | None,
    turns: list[uuid.UUID],
    *,
    now: datetime = _NOW,
) -> None:
    def provider(_request):
        raise AssertionError("Worker completion must never fall back to a provider lookup")

    async def legacy_context(_row: TaskContinuationRow):
        raise AssertionError("Worker completion must never use human-history supersession")

    async def notice(_row: TaskContinuationRow, _text: str, _reason: str) -> None:
        raise AssertionError("No notice is requested by these host decisions")

    async def resume(row: TaskContinuationRow, _decision: ContinuationDecision) -> None:
        turns.append(row.idempotency_key)

    async with build_fake_anthropic(provider) as client:
        await dispatch_pending_continuations(
            sm,
            client,
            tenant_id=run.tenant_id,
            platform="discord",
            thread_id=run.thread_id,
            run_decision=resume,
            notice=notice,
            decision_context=legacy_context,
            worker_finished_decision=decision,
            now=lambda: now,
        )


@pytest.mark.parametrize("mode", ["absent", "unready", "busy"])
async def test_unavailable_or_busy_host_decision_defers_without_native_send(
    pooled: async_sessionmaker[AsyncSession], mode: str
) -> None:
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)

    async def decision(_row: TaskContinuationRow, _now: datetime) -> ContinuationDecision | None:
        return ContinuationDecision(action="skip_turn_running") if mode == "busy" else None

    turns: list[uuid.UUID] = []
    await _dispatch(pooled, run, None if mode == "absent" else decision, turns)
    row = await _read(pooled, run)
    assert turns == [] and row is not None
    assert row.status == "pending" and row.attempts == 0 and row.started_at is None
    assert row.available_at == _NOW + WAKE_RETRY_DELAY


async def test_restart_and_two_dispatchers_resume_at_most_once(
    pooled: async_sessionmaker[AsyncSession],
    db_engine: AsyncEngine,
) -> None:
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)
    await db_engine.dispose()
    schema = isolated_test_schema(db_engine)
    assert schema is not None
    engines = [
        build_test_engine(db_engine.url.render_as_string(hide_password=False), schema)
        for _ in range(2)
    ]
    factories = [async_sessionmaker(engine, expire_on_commit=False) for engine in engines]
    for factory in factories:
        require_owned_sessionmaker(factory)

    async def decision(_row: TaskContinuationRow, _now: datetime) -> ContinuationDecision:
        return ContinuationDecision(
            action="dispatch", seed_user_message="Collect the completed workers."
        )

    turns: list[uuid.UUID] = []
    try:
        await asyncio.gather(*(_dispatch(factory, run, decision, turns) for factory in factories))
        await asyncio.gather(*(_dispatch(factory, run, decision, turns) for factory in factories))
        assert turns == [worker_finished_key(run.id)]
        row = await _read(factories[0], run)
        assert row is not None and row.status == "delivered" and row.attempts == 1
    finally:
        for engine in engines:
            await engine.dispose()


async def test_expired_started_worker_wake_never_replays(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)
    claim = await claim_wake(pooled, idempotency_key=worker_finished_key(run.id), now=_NOW)
    assert claim is not None and await start_wake(pooled, claim, now=_NOW)
    expired = _NOW + WAKE_RUN_LEASE + timedelta(seconds=1)
    abandoned = await abandon_interrupted_wakes(pooled, platform="discord", now=expired)
    assert [row.idempotency_key for row in abandoned] == [worker_finished_key(run.id)]
    assert not await settle_wake(pooled, claim, status="delivered", now=expired)
    turns: list[uuid.UUID] = []
    await _dispatch(pooled, run, None, turns, now=expired)
    row = await _read(pooled, run)
    assert row is not None and row.status == "skipped" and row.skip_reason == "interrupted"
    assert turns == []


@pytest.fixture(scope="module")
def prior_queue_reader():
    path = Path(__file__).parent / "fixtures/continuation_prior.txt"
    source = path.read_bytes()
    assert (
        hashlib.sha256(source).hexdigest()
        == "cd530c55e21110be5f223a7b82b142307a5a5046bdd8bb7f48a740a33880c89f"
    )
    module = types.ModuleType("prior_continuation_queue_reader")
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
        yield module
    finally:
        del sys.modules[module.__name__]


@pytest.mark.parametrize("entrypoint", ["worker", "wake", "continuation"])
async def test_previous_reader_can_dispatch_legacy_timer_after_refused_new_write(
    pooled: async_sessionmaker[AsyncSession],
    prior_queue_reader,
    entrypoint: str,
) -> None:
    run = await _run(pooled)
    request = _request(run)
    timer = request.model_copy(update={"reason": "timer", "idempotency_key": uuid.uuid4()})
    await enqueue_wake(pooled, timer, available_at=_NOW)
    if entrypoint == "worker":
        async with pooled.begin() as session:
            assert not await enqueue_worker_finished(session, request, run_id=run.id, now=_NOW)
    else:
        with pytest.raises(ContinuationReasonUnavailable):
            if entrypoint == "wake":
                await enqueue_wake(pooled, request, available_at=_NOW)
            else:
                await record_continuation(pooled, request)
    assert await _read(pooled, run) is None
    async with pooled.begin() as session:
        pending = await prior_queue_reader.list_pending_continuations(
            session,
            tenant_id=run.tenant_id,
            platform=run.platform,
            thread_id=run.thread_id,
        )
        dispatchable = await prior_queue_reader.list_dispatchable_continuations(
            session,
            tenant_id=run.tenant_id,
            platform=run.platform,
            thread_id=run.thread_id,
            now=_NOW,
            max_attempts=5,
        )
        due = await prior_queue_reader.list_due_wake_rows(
            session,
            platform=run.platform,
            now=_NOW,
            max_attempts=5,
            limit=1,
        )
        for rows in (pending, dispatchable, due):
            assert [row.idempotency_key for row in rows] == [timer.idempotency_key]
            assert rows[0].reason == "timer" and rows[0].attempts == 0
        claimed = await prior_queue_reader.claim_wake_row(
            session,
            idempotency_key=timer.idempotency_key,
            owner="old-process",
            now=_NOW,
            lease=timedelta(minutes=1),
            max_attempts=5,
        )
        assert claimed is not None and claimed.reason == "timer" and claimed.attempts == 1
        assert await prior_queue_reader.start_wake_row(
            session,
            idempotency_key=timer.idempotency_key,
            owner="old-process",
            now=_NOW,
            lease=timedelta(minutes=1),
        )
        assert await prior_queue_reader.settle_wake_row(
            session,
            idempotency_key=timer.idempotency_key,
            owner="old-process",
            status="delivered",
            now=_NOW,
        )
        actual = await get_continuation(session, idempotency_key=timer.idempotency_key)
        assert actual is not None and actual.status == "delivered"
        assert actual.started_at == _NOW and actual.delivered_at == _NOW
    current, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and workers == ()


async def test_previous_reader_negative_control_still_rejects_injected_future_reason(
    pooled: async_sessionmaker[AsyncSession],
    prior_queue_reader,
) -> None:
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)
    timer = _request(run).model_copy(update={"reason": "timer", "idempotency_key": uuid.uuid4()})
    await enqueue_wake(pooled, timer, available_at=_NOW)
    async with pooled() as session:
        with pytest.raises(ValidationError, match="reason"):
            await prior_queue_reader.list_dispatchable_continuations(
                session,
                tenant_id=run.tenant_id,
                platform=run.platform,
                thread_id=run.thread_id,
                now=_NOW,
                max_attempts=5,
            )


async def _arm(session, run, workers, *, now=_NOW):
    ids = tuple(w.id for w in workers)
    return await arm_worker_finished(
        session,
        _request(run).model_copy(update={"idempotency_key": worker_finished_key(run.id, ids)}),
        run_id=run.id,
        worker_ids=ids,
        now=now,
    )


async def _finish_batch(session, run, worker, *, now=_NOW):
    await fleet.run_status(session, tenant_id=run.tenant_id, run_id=run.id, for_update=True)
    await fleet.finish(
        session,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=worker.version,
        status="cancelled",
        error_code="stopped",
    )
    return await enqueue_worker_finished(session, tenant_id=run.tenant_id, run_id=run.id, now=now)


@pytest.mark.parametrize("quorum", [None, 1])
async def test_frozen_initial_and_review_waits_do_not_count_each_others_workers(pooled, quorum):
    run = await _run(pooled, quorum=quorum, max_workers=4)
    first, late = [await _worker(pooled, run, i) for i in range(2)]
    async with pooled.begin() as db:
        initial = await _arm(db, run, [first, late])
        assert initial is not None
        assert await _arm(db, run, [late, first]) == initial
        changed = _request(run).model_copy(
            update={"idempotency_key": initial.idempotency_key, "target_name": "other"}
        )
        with pytest.raises(ValueError, match="different frozen intent"):
            await arm_worker_finished(
                db, changed, run_id=run.id, worker_ids=[first.id, late.id], now=_NOW
            )
    assert not await list_dispatchable_wakes(
        pooled, tenant_id=run.tenant_id, platform=run.platform, thread_id=run.thread_id, now=_NOW
    )
    async with pooled.begin() as db:
        assert await _finish_batch(db, run, first) is (quorum == 1)
    review = await _worker(pooled, run, 2, reviewer_of=first.id)
    other_review = await _worker(pooled, run, 3, reviewer_of=late.id)
    async with pooled.begin() as db:
        with pytest.raises(ValueError, match="separate batches"):
            await _arm(db, run, [first, review])
        second = await _arm(db, run, [review, other_review])
        assert second is not None and second.idempotency_key != initial.idempotency_key
        batch = FrozenWorkerBatch.model_validate_json(second.requested_work)
        assert set(batch.worker_ids) == {review.id, other_review.id} and batch.quorum == 2
        assert batch.binding_id == run.orchestrator_binding_id
        assert batch.session_id == run.orchestrator_session_id
        assert await _finish_batch(db, run, late) is (quorum is None)
        assert not await enqueue_worker_finished(
            db, tenant_id=run.tenant_id, run_id=run.id, now=_NOW
        )
    async with pooled.begin() as db:
        assert not await _finish_batch(db, run, review)
        assert await _finish_batch(db, run, other_review)
        assert not await enqueue_worker_finished(
            db, tenant_id=run.tenant_id, run_id=run.id, now=_NOW
        )
    rows = await list_dispatchable_wakes(
        pooled, tenant_id=run.tenant_id, platform=run.platform, thread_id=run.thread_id, now=_NOW
    )
    assert {r.idempotency_key for r in rows} == {initial.idempotency_key, second.idempotency_key}
    current, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and current.status == "running"


@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_armed_wait_deadline_never_rolls_back_terminal_move(pooled, offset):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    async with pooled.begin() as db:
        row = await _arm(db, run, [worker])
    async with pooled.begin() as db:
        assert await _finish_batch(
            db, run, worker, now=run.deadline_at + timedelta(seconds=offset)
        ) is (offset < 0)
    _, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert workers[0].status == "cancelled" and workers[0].version == worker.version + 1
    async with pooled() as db:
        actual = await get_continuation(db, idempotency_key=row.idempotency_key)
    assert (actual.available_at == row.available_at) is (offset >= 0)


async def test_arm_and_completion_roll_back_and_completed_before_arm_is_not_lost(pooled):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    async with pooled() as db, db.begin():
        assert not await _finish_batch(db, run, worker)
        row = await _arm(db, run, [worker])
        assert row.available_at == _NOW
        await db.rollback()
    async with pooled() as db:
        assert await get_continuation(db, idempotency_key=row.idempotency_key) is None
    async with pooled.begin() as db:
        assert not await _finish_batch(db, run, worker)
    async with pooled.begin() as db:
        row = await _arm(db, run, [worker])
        assert row.available_at == _NOW


@pytest.mark.parametrize("mutation", ["account", "channel", "key", "duplicate", "foreign"])
async def test_freezing_refuses_changed_address_identity_or_members(pooled, mutation):
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    ids = [worker.id]
    request = _request(run).model_copy(update={"idempotency_key": worker_finished_key(run.id, ids)})
    if mutation == "account":
        request = request.model_copy(update={"requester_account_id": uuid.uuid4()})
    elif mutation == "channel":
        request = request.model_copy(update={"parent_channel_id": "other"})
    elif mutation == "key":
        request = request.model_copy(update={"idempotency_key": uuid.uuid4()})
    elif mutation == "duplicate":
        ids.append(worker.id)
    else:
        ids = [uuid.uuid4()]
        request = request.model_copy(update={"idempotency_key": worker_finished_key(run.id, ids)})
    async with pooled.begin() as db:
        with pytest.raises(ValueError):
            await arm_worker_finished(db, request, run_id=run.id, worker_ids=ids, now=_NOW)
    assert await _read(pooled, run) is None


@pytest.mark.parametrize("quorum", [None, 1])
async def test_racing_terminal_moves_publish_once_and_restart_keeps_the_fence(
    pooled, db_engine, quorum
):
    run = await _run(pooled, quorum=quorum)
    workers = [await _worker(pooled, run, i) for i in range(2)]
    async with pooled.begin() as db:
        row = await _arm(db, run, workers)

    async def complete(worker):
        async with pooled.begin() as db:
            return await _finish_batch(db, run, worker)

    assert sum(await asyncio.gather(*(complete(w) for w in workers))) == 1
    claim = await claim_wake(pooled, idempotency_key=row.idempotency_key, now=_NOW)
    takeover_at = _NOW + timedelta(minutes=6)
    schema = isolated_test_schema(db_engine)
    assert schema is not None
    engine = build_test_engine(db_engine.url.render_as_string(hide_password=False), schema)
    restarted = async_sessionmaker(engine, expire_on_commit=False)
    require_owned_sessionmaker(restarted)
    try:
        reclaimed = await claim_wake(
            restarted, idempotency_key=row.idempotency_key, now=takeover_at
        )
        assert reclaimed is not None and reclaimed.owner != claim.owner
        assert not await start_wake(pooled, claim, now=takeover_at)
        assert await start_wake(restarted, reclaimed, now=takeover_at)
        assert not await claim_wake(
            pooled,
            idempotency_key=row.idempotency_key,
            now=takeover_at + WAKE_RUN_LEASE + timedelta(seconds=1),
        )
    finally:
        await engine.dispose()
