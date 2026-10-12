"""Worker completion uses real fleet rows and the existing leased wake ladder."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core.continuity.continuation import ContinuationDecision, ContinuationRequest
from daimon.core.continuity.dispatch import WorkerFinishedDecision, dispatch_pending_continuations
from daimon.core.continuity.wakes import (
    WAKE_RETRY_DELAY,
    WAKE_RUN_LEASE,
    abandon_interrupted_wakes,
    claim_wake,
    enqueue_wake,
    enqueue_worker_finished,
    settle_wake,
    start_wake,
    worker_finished_key,
)
from daimon.core.fleet import FleetConflict, FleetRun, FleetWorker
from daimon.core.stores import fleet
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.testing.db import build_test_engine, isolated_test_schema, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_NOW = datetime(2026, 10, 12, 18, tzinfo=UTC)


@pytest.fixture
def pooled(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    require_owned_sessionmaker(factory)
    return factory


async def _run(sm: async_sessionmaker[AsyncSession], *, quorum: int | None = None) -> FleetRun:
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
        deadline_at=_NOW + timedelta(hours=1),
        max_workers=2,
        created_by="authorized-host",
        quorum=quorum,
    )


async def _worker(sm: async_sessionmaker[AsyncSession], run: FleetRun, ordinal: int) -> FleetWorker:
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


async def _complete(
    sm: async_sessionmaker[AsyncSession], run: FleetRun, worker: FleetWorker
) -> bool:
    async with sm.begin() as session:
        await fleet.run_status(session, tenant_id=run.tenant_id, run_id=run.id, for_update=True)
        await fleet.finish(
            session,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=worker.version,
            status="cancelled",
            error_code="cancelled",
        )
        return await enqueue_worker_finished(session, _request(run), run_id=run.id, now=_NOW)


async def _read(sm: async_sessionmaker[AsyncSession], run: FleetRun) -> TaskContinuationRow | None:
    async with sm() as session:
        return await get_continuation(session, idempotency_key=worker_finished_key(run.id))


@pytest.mark.parametrize("quorum", [None, 1])
async def test_actual_all_or_quorum_readiness_queues_once_without_claiming_success(
    pooled: async_sessionmaker[AsyncSession], quorum: int | None
) -> None:
    run = await _run(pooled, quorum=quorum)
    workers = [await _worker(pooled, run, ordinal) for ordinal in range(2)]
    assert await _complete(pooled, run, workers[0]) is (quorum == 1)
    assert await _complete(pooled, run, workers[1]) is (quorum is None)
    current, finished = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current.status == "collecting" and current.version == run.version + 1
    assert all(worker.status == "cancelled" and worker.result is None for worker in finished)
    row = await _read(pooled, run)
    assert row is not None and row.reason == "worker_finished" and row.status == "pending"
    assert row.available_at == _NOW and row.attempts == 0
    async with pooled.begin() as session:
        assert not await enqueue_worker_finished(session, _request(run), run_id=run.id, now=_NOW)
    assert await _read(pooled, run) == row


async def test_terminal_transition_and_wake_roll_back_together(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    with pytest.raises(RuntimeError, match="crash before commit"):
        async with pooled.begin() as session:
            await fleet.run_status(session, tenant_id=run.tenant_id, run_id=run.id, for_update=True)
            await fleet.finish(
                session,
                tenant_id=run.tenant_id,
                worker_id=worker.id,
                expected_version=worker.version,
                status="cancelled",
                error_code="cancelled",
            )
            assert await enqueue_worker_finished(session, _request(run), run_id=run.id, now=_NOW)
            raise RuntimeError("crash before commit")
    current, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and workers == (worker,)
    assert await _read(pooled, run) is None


async def test_racing_completions_commit_one_wake(pooled: async_sessionmaker[AsyncSession]) -> None:
    run = await _run(pooled)
    workers = [await _worker(pooled, run, ordinal) for ordinal in range(2)]
    assert sorted(
        await asyncio.gather(*(_complete(pooled, run, worker) for worker in workers))
    ) == [False, True]
    row = await _read(pooled, run)
    assert row is not None and row.status == "pending"


@pytest.mark.parametrize(
    "field", ["tenant_id", "requester_account_id", "platform", "parent_channel_id", "thread_id"]
)
async def test_foreign_run_or_address_cannot_queue_work(
    pooled: async_sessionmaker[AsyncSession], field: str
) -> None:
    run = await _run(pooled)
    await _worker(pooled, run, 0)
    data = _request(run).model_dump()
    data[field] = (
        uuid.uuid4()
        if field in ("tenant_id", "requester_account_id")
        else "slack"
        if field == "platform"
        else "foreign"
    )
    request = ContinuationRequest.model_validate(data)
    with pytest.raises(FleetConflict):
        async with pooled.begin() as session:
            await enqueue_worker_finished(session, request, run_id=run.id, now=_NOW)
    current, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and await _read(pooled, run) is None


async def test_wrong_key_or_inactive_transaction_refuses_before_autobegin(
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


async def test_missing_collecting_wake_is_an_invariant_refusal(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    await fleet.set_run_status(
        pooled,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=run.version,
        status="collecting",
    )
    with pytest.raises(FleetConflict, match="expected completion wake"):
        async with pooled.begin() as session:
            await enqueue_worker_finished(session, _request(run), run_id=run.id, now=_NOW)
    assert await _read(pooled, run) is None


async def test_changed_request_cannot_reuse_a_collecting_wake(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    await _complete(pooled, run, await _worker(pooled, run, 0))
    original = await _read(pooled, run)
    changed = _request(run).model_copy(update={"requested_work": "Run different billed work."})
    with pytest.raises(FleetConflict, match="expected completion wake"):
        async with pooled.begin() as session:
            await enqueue_worker_finished(session, changed, run_id=run.id, now=_NOW)
    assert await _read(pooled, run) == original


async def test_duplicate_insert_rolls_back_terminal_and_collecting_transitions(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    other = _request(run).model_copy(update={"reason": "task_handoff"})
    await enqueue_wake(pooled, other, available_at=_NOW)
    original = await _read(pooled, run)
    with pytest.raises(IntegrityError):
        await _complete(pooled, run, worker)
    current, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and workers == (worker,)
    assert await _read(pooled, run) == original


async def test_readiness_at_deadline_refuses_and_rolls_back_the_terminal_transition(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled)
    worker = await _worker(pooled, run, 0)
    with pytest.raises(FleetConflict, match="deadline"):
        async with pooled.begin() as session:
            await fleet.run_status(session, tenant_id=run.tenant_id, run_id=run.id, for_update=True)
            await fleet.finish(
                session,
                tenant_id=run.tenant_id,
                worker_id=worker.id,
                expected_version=worker.version,
                status="cancelled",
                error_code="cancelled",
            )
            await enqueue_worker_finished(
                session, _request(run), run_id=run.id, now=run.deadline_at
            )
    current, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current == run and workers == (worker,)
    assert await _read(pooled, run) is None


async def test_collecting_freezes_membership_without_claiming_financial_admission(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled, quorum=1)
    worker = await _worker(pooled, run, 0)
    assert await _complete(pooled, run, worker)
    with pytest.raises(FleetConflict, match="collecting"):
        await _worker(pooled, run, 1)
    current, workers = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert current.reserved_usd == Decimal(0) and current.observed_usd == Decimal(0)
    assert len(workers) == 1 and workers[0].reserved_usd == Decimal(0)


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
    await _complete(pooled, run, await _worker(pooled, run, 0))

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
    await _complete(pooled, run, await _worker(pooled, run, 0))
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
    await _complete(pooled, run, await _worker(pooled, run, 0))
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
