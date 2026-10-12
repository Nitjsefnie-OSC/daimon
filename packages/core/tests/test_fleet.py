"""Fleet store: the run/worker rows, the tenant worker pool, results in the journal."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from daimon.core.fleet import (
    FleetConflict,
    FleetRun,
    FleetWorker,
    ResultPointer,
    append_result,
    result_digest,
    run_settled,
    spawn_operation_key,
    worker_slot,
)
from daimon.core.stores import fleet, mux_state
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.fleet import WorkerPool
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.contracts.resources import ProviderBinding
from mux.state.journal import JournalEntry
from mux.state.memory import MemoryStateStore
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
# Deadlines are against the database clock, so they sit well after the real now.
LATER = datetime.now(UTC) + timedelta(days=1)
SPEC: dict[str, Any] = {"backend": "openai", "model": "gpt-6-luna", "system": "Research."}


@pytest.fixture
async def pooled(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    """Pooled connections of their own, so concurrent claims really race."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def _run(
    sm: async_sessionmaker[AsyncSession],
    *,
    max_workers: int = 4,
    budget: str = "10",
    key: str = "op-1",
    quorum: int | None = None,
    tenant: TenantRow | None = None,
) -> FleetRun:
    async with sm.begin() as session:
        tenant = tenant or await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    return await fleet.create_run(
        sm,
        tenant_id=tenant.id,
        orchestrator_account_id=account.id,
        orchestrator_session_id="sess_orchestrator",
        orchestrator_binding_id="bind_orchestrator",
        platform="discord",
        channel_id="900000000000000001",
        thread_id="900000000000000002",
        operation_key=key,
        total_budget_usd=Decimal(budget),
        deadline_at=LATER,
        max_workers=max_workers,
        created_by="mcp:orchestrator",
        quorum=quorum,
    )


async def _worker(
    sm: async_sessionmaker[AsyncSession],
    run: FleetRun,
    ordinal: int,
    *,
    digest: str = "d1",
    reviewer_of: uuid.UUID | None = None,
) -> FleetWorker:
    return await fleet.add_worker(
        sm,
        tenant_id=run.tenant_id,
        run_id=run.id,
        ordinal=ordinal,
        role="research",
        spec=SPEC,
        spec_digest=digest,
        backend="openai",
        profile="openai.persistent_workspace",
        model="gpt-6-luna",
        budget_usd=Decimal("2"),
        deadline_at=LATER,
        max_turns=3,
        reviewer_of=reviewer_of,
    )


# Pure rules


def _status_worker(status: str, **extra: Any) -> FleetWorker:
    base = dict(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        ordinal=0,
        role="r",
        spec={},
        spec_digest="d",
        backend="openai",
        profile="p",
        model="m",
        binding_id=None,
        status=status,
        budget_usd=Decimal(1),
        reserved_usd=Decimal(0),
        deadline_at=LATER,
        max_turns=1,
        turns_used=0,
        attempts=0,
        reviewer_of=None,
        result_contract=None,
        result=None,
        error_code=None,
        version=1,
    )
    return FleetWorker(**{**base, **extra})  # type: ignore[arg-type]


def _plain_run(**overrides: Any) -> FleetRun:
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        orchestrator_account_id=uuid.uuid4(),
        orchestrator_session_id="s",
        orchestrator_binding_id="b",
        platform="discord",
        channel_id="c",
        thread_id="t",
        status="running",
        total_budget_usd=Decimal(1),
        reserved_usd=Decimal(0),
        observed_usd=Decimal(0),
        deadline_at=LATER,
        max_workers=1,
        quorum=None,
        version=1,
    )
    return FleetRun(**{**base, **overrides})


def test_a_run_settles_when_every_worker_ends_or_its_quorum_does() -> None:
    run = _plain_run(max_workers=3)
    done, failed, live = (
        _status_worker("succeeded"),
        _status_worker("failed"),
        _status_worker("running"),
    )
    assert not run_settled(run, [])
    assert not run_settled(run, [done, live])
    assert run_settled(run, [done, failed])
    quorum = _plain_run(max_workers=3, quorum=1)
    assert run_settled(quorum, [done, live])
    assert not run_settled(quorum, [live])


def test_a_worker_slot_is_private_to_the_orchestrator_and_one_per_ordinal() -> None:
    run = _plain_run(max_workers=2)
    first, second = _status_worker("pending"), _status_worker("pending", ordinal=1)
    slot = worker_slot(run, first)
    assert slot.account_id == str(run.orchestrator_account_id)  # never the shared slot
    assert slot.thread.thread_id == f"fleet/{run.id}/0"
    assert slot.thread.channel.tenant_id == str(run.tenant_id)
    assert worker_slot(run, second) != slot
    assert spawn_operation_key(run.id, 1) == f"fleet:{run.id}:spawn:1"


# Results in the worker's own journal


def _final_message(session_id: str, text: str) -> Event:
    return Event.model_validate(
        {
            "id": "ev_final",
            "session_id": session_id,
            "sequence": 0,
            "type": "agent.message",
            "turn_id": "turn_1",
            "observed_at": NOW,
            "authority": "record",
            "payload": {"item_id": "m1", "content": [{"type": "text", "text": text}]},
            "native": NativeProvenance(provider="openai", api_revision="v1"),
        }
    )


@pytest.fixture(params=["memory", "postgres"])
async def bound(
    request: pytest.FixtureRequest, db_session_factory: async_sessionmaker[AsyncSession]
) -> tuple[Any, ...]:
    """A worker's bound session and lease, on the memory store or Postgres."""
    if request.param == "memory":
        store: Any = MemoryStateStore()
        run = _plain_run()
    else:
        store = PostgresStateStore(db_session_factory, trust_caller_clock=True)
        run = await _run(db_session_factory)
    worker = _status_worker("running", run_id=run.id, tenant_id=run.tenant_id)
    slot = worker_slot(run, worker)
    session = ResourceRef(id="sess_w0", kind="session", provider="openai", account_scope_id="p")
    await store.put_binding(
        ProviderBinding(
            id=f"bind_{uuid.uuid4()}",
            thread=slot.thread,
            provider="openai",
            profile="openai.persistent_workspace",
            native_refs={"session": session.id},
            generation=1,
            config_revision=1,
            legacy_account_id=slot.account_id,
        ),
        expected_generation=0,
    )
    lease = await store.acquire_lease(
        slot, holder="p1", turn_id="turn_1", now=NOW, ttl=timedelta(minutes=5)
    )
    return store, worker, slot, session, lease


async def _successor(store: Any, slot: Any) -> Any:
    later = NOW + timedelta(minutes=6)
    lease = await store.acquire_lease(
        slot, holder="p2", turn_id="turn_1", now=later, ttl=timedelta(minutes=5)
    )
    assert lease.took_over
    return lease


async def test_a_result_after_the_turn_journaled_its_final_message_is_one_entry_of_its_own(
    bound: tuple[Any, ...],
) -> None:
    store, worker, slot, session, lease = bound
    final = _final_message(session.id, "the answer")
    # The worker turn already journaled its final message under its native key.
    await store.append_events(
        session,
        [JournalEntry(source_key="native:m1", event=final)],
        fence=lease,
        cursor="c0",
        now=NOW,
    )

    first = await append_result(store, session, worker, final, lease=lease, cursor="c1", now=NOW)
    successor = await _successor(store, slot)
    again = await append_result(
        store, session, worker, final, lease=successor, cursor="c1", now=NOW + timedelta(minutes=6)
    )

    assert again == first
    ids = [event.id for event in await store.read_events(session.id)]
    assert ids == ["ev_final", f"fleet:{worker.id}:result"]  # no event id twice
    assert first.sequence == 1 and first.excerpt == "the answer"
    assert first.digest == result_digest(dict(final.payload))


async def test_a_successor_with_a_rebuilt_event_gets_the_stored_result(
    bound: tuple[Any, ...],
) -> None:
    store, worker, slot, session, lease = bound
    first = await append_result(
        store,
        session,
        worker,
        _final_message(session.id, "the answer"),
        lease=lease,
        cursor="c1",
        now=NOW,
    )
    successor = await _successor(store, slot)
    rebuilt = _final_message(session.id, "the answer, reconstructed").model_copy(
        update={"id": "ev_rebuilt"}
    )

    again = await append_result(
        store,
        session,
        worker,
        rebuilt,
        lease=successor,
        cursor="c1",
        now=NOW + timedelta(minutes=6),
    )

    assert again == first  # the first stored result stands
    assert [event.id for event in await store.read_events(session.id)] == [
        f"fleet:{worker.id}:result"
    ]


# The store


async def test_a_run_and_its_workers_are_idempotent_by_key_and_ordinal(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    again = await fleet.create_run(
        db_session_factory,
        tenant_id=run.tenant_id,
        orchestrator_account_id=run.orchestrator_account_id,
        orchestrator_session_id="other",
        orchestrator_binding_id="other",
        platform="discord",
        channel_id="c",
        thread_id="t",
        operation_key="op-1",
        total_budget_usd=Decimal("99"),
        deadline_at=LATER,
        max_workers=1,
        created_by="x",
    )
    assert again == run

    worker = await _worker(db_session_factory, run, 0)
    assert await _worker(db_session_factory, run, 0) == worker
    with pytest.raises(FleetConflict, match="another spec"):
        await _worker(db_session_factory, run, 0, digest="d2")
    with pytest.raises(FleetConflict, match="allows 4 workers"):
        await _worker(db_session_factory, run, 4)
    with pytest.raises(FleetConflict, match="outlast"):
        await fleet.add_worker(
            db_session_factory,
            tenant_id=run.tenant_id,
            run_id=run.id,
            ordinal=1,
            role="r",
            spec=SPEC,
            spec_digest="d",
            backend="openai",
            profile="p",
            model="m",
            budget_usd=Decimal(1),
            deadline_at=LATER + timedelta(seconds=1),
            max_turns=1,
        )


async def test_a_reviewer_must_be_another_worker_of_the_same_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    run = await _run(db_session_factory, tenant=tenant)
    other = await _run(db_session_factory, key="op-2", tenant=tenant)
    reviewed = await _worker(db_session_factory, run, 0)
    reviewer = await _worker(db_session_factory, run, 1, reviewer_of=reviewed.id)
    assert reviewer.reviewer_of == reviewed.id
    elsewhere = await _worker(db_session_factory, other, 0)
    with pytest.raises(IntegrityError):  # a worker of another run cannot be reviewed
        await _worker(db_session_factory, run, 2, reviewer_of=elsewhere.id)


async def test_a_worker_moves_only_along_its_transitions_under_a_version_cas(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    worker = await _worker(db_session_factory, run, 0)
    sm, tenant = db_session_factory, run.tenant_id

    with pytest.raises(FleetConflict):  # pending cannot run before it is claimed
        await fleet.mark_running(
            sm, tenant_id=tenant, worker_id=worker.id, expected_version=1, binding_id="b"
        )
    claimed = await fleet.claim_worker(sm, tenant_id=tenant, run_id=run.id, cap=3)
    assert claimed is not None and claimed.status == "starting" and claimed.version == 2
    with pytest.raises(FleetConflict):  # a stale version loses
        await fleet.mark_running(
            sm, tenant_id=tenant, worker_id=worker.id, expected_version=1, binding_id="b"
        )
    running = await fleet.mark_running(
        sm, tenant_id=tenant, worker_id=worker.id, expected_version=2, binding_id="bind_w0"
    )
    assert running.binding_id == "bind_w0"
    pointer = await _memory_result(run, running)
    done = await fleet.record_result(
        sm, tenant_id=tenant, worker_id=worker.id, expected_version=3, result=pointer
    )
    assert done.status == "succeeded" and done.result == pointer
    for status in ("failed", "cancelled", "pending"):
        with pytest.raises(FleetConflict):  # a terminal worker never moves
            await fleet.finish(
                sm,
                tenant_id=tenant,
                worker_id=worker.id,
                expected_version=4,
                status=status,  # type: ignore[arg-type]
                error_code="x",
            )


async def _memory_result(run: FleetRun, worker: FleetWorker) -> ResultPointer:
    store = MemoryStateStore()
    slot = worker_slot(run, worker)
    session = ResourceRef(id="sess_w0", kind="session", provider="openai", account_scope_id="p")
    await store.put_binding(
        ProviderBinding(
            id="bind_w0",
            thread=slot.thread,
            provider="openai",
            profile="openai.persistent_workspace",
            native_refs={"session": session.id},
            generation=1,
            config_revision=1,
            legacy_account_id=slot.account_id,
        ),
        expected_generation=0,
    )
    lease = await store.acquire_lease(slot, holder="p", turn_id="t", now=NOW, ttl=timedelta(1))
    event = _final_message(session.id, "ok")
    return await append_result(store, session, worker, event, lease=lease, cursor="c", now=NOW)


async def test_two_claims_cannot_both_win_one_worker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    await _worker(db_session_factory, run, 0)
    first = await fleet.claim_worker(
        db_session_factory, tenant_id=run.tenant_id, run_id=run.id, cap=3
    )
    second = await fleet.claim_worker(
        db_session_factory, tenant_id=run.tenant_id, run_id=run.id, cap=3
    )
    assert first is not None and second is None


async def test_the_tenant_pool_never_exceeds_its_cap_under_concurrent_claims(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(pooled, max_workers=8)
    for ordinal in range(8):
        await _worker(pooled, run, ordinal)
    claims = await asyncio.gather(
        *(
            fleet.claim_worker(pooled, tenant_id=run.tenant_id, run_id=run.id, cap=3)
            for _ in range(8)
        )
    )
    won = [claim for claim in claims if claim is not None]
    assert len(won) == 3
    assert len({claim.id for claim in won}) == 3
    async with pooled() as session:
        active = (
            await session.execute(
                text(
                    "SELECT count(*) FROM fleet_worker "
                    "WHERE tenant_id = :t AND status IN ('starting', 'running')"
                ),
                {"t": run.tenant_id},
            )
        ).scalar_one()
    assert active == 3

    # Ending one frees exactly one place.
    await fleet.finish(
        pooled,
        tenant_id=run.tenant_id,
        worker_id=won[0].id,
        expected_version=won[0].version,
        status="cancelled",
        error_code="stopped",
    )
    assert await fleet.claim_worker(pooled, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert await fleet.claim_worker(pooled, tenant_id=run.tenant_id, run_id=run.id, cap=3) is None


async def test_the_cap_is_per_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mine, theirs = await _run(db_session_factory), await _run(db_session_factory, key="op-x")
    await _worker(db_session_factory, mine, 0)
    await _worker(db_session_factory, theirs, 0)
    assert await fleet.claim_worker(
        db_session_factory, tenant_id=mine.tenant_id, run_id=mine.id, cap=1
    )
    assert await fleet.claim_worker(
        db_session_factory, tenant_id=theirs.tenant_id, run_id=theirs.id, cap=1
    )


async def test_the_process_pool_gives_its_slot_back_when_the_tenant_has_no_room(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    await _worker(db_session_factory, run, 0)
    await _worker(db_session_factory, run, 1)
    pool = WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=5)

    claimed = await pool.claim(db_session_factory, tenant_id=run.tenant_id, run_id=run.id)
    assert claimed is not None
    # The process is full: no tenant slot is even asked for.
    assert await pool.claim(db_session_factory, tenant_id=run.tenant_id, run_id=run.id) is None
    pool.release()
    assert await pool.claim(db_session_factory, tenant_id=run.tenant_id, run_id=run.id)

    empty = WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=5)
    other = await _run(db_session_factory, key="op-empty")
    assert await empty.claim(db_session_factory, tenant_id=other.tenant_id, run_id=other.id) is None
    # Refused by the tenant side (no pending worker), so the process slot came back.
    assert await empty.claim(db_session_factory, tenant_id=run.tenant_id, run_id=run.id) is None


async def test_a_lost_worker_is_requeued_until_its_attempts_run_out(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    await _worker(db_session_factory, run, 0)
    sm, tenant = db_session_factory, run.tenant_id
    claimed = await fleet.claim_worker(sm, tenant_id=tenant, run_id=run.id, cap=3)
    assert claimed is not None
    requeued = await fleet.requeue(sm, tenant_id=tenant, worker=claimed, max_attempts=2)
    assert requeued.status == "pending" and requeued.attempts == 1
    again = await fleet.claim_worker(sm, tenant_id=tenant, run_id=run.id, cap=3)
    assert again is not None
    lost = await fleet.requeue(sm, tenant_id=tenant, worker=again, max_attempts=2)
    assert (lost.status, lost.error_code) == ("failed", "worker_lost")


async def test_reservations_stay_within_the_worker_and_the_run_budget(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory, budget="3")
    first = await _worker(db_session_factory, run, 0)
    second = await _worker(db_session_factory, run, 1)
    sm, tenant = db_session_factory, run.tenant_id
    with pytest.raises(FleetConflict, match="worker's budget"):
        await fleet.reserve(
            sm, tenant_id=tenant, worker_id=first.id, expected_version=1, usd=Decimal("2.5")
        )
    first = await fleet.reserve(
        sm, tenant_id=tenant, worker_id=first.id, expected_version=1, usd=Decimal("2")
    )
    with pytest.raises(FleetConflict, match="total budget"):
        await fleet.reserve(
            sm, tenant_id=tenant, worker_id=second.id, expected_version=1, usd=Decimal("1.5")
        )
    await fleet.reserve(
        sm, tenant_id=tenant, worker_id=second.id, expected_version=1, usd=Decimal("1")
    )
    # Lowering a reservation gives the difference back to the run.
    await fleet.reserve(
        sm, tenant_id=tenant, worker_id=first.id, expected_version=first.version, usd=Decimal("0.5")
    )
    refreshed, _ = await fleet.run_status(sm, tenant_id=tenant, run_id=run.id)
    assert refreshed.reserved_usd == Decimal("1.5")


async def test_a_run_moves_along_its_transitions_and_records_its_end(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    sm, tenant = db_session_factory, run.tenant_id
    with pytest.raises(FleetConflict):
        await fleet.set_run_status(
            sm, tenant_id=tenant, run_id=run.id, expected_version=1, status="completed"
        )
    collecting = await fleet.set_run_status(
        sm, tenant_id=tenant, run_id=run.id, expected_version=1, status="collecting"
    )
    done = await fleet.set_run_status(
        sm, tenant_id=tenant, run_id=run.id, expected_version=collecting.version, status="completed"
    )
    assert done.status == "completed"
    with pytest.raises(FleetConflict, match="is completed"):
        await _worker(sm, done, 0)


async def test_another_tenant_cannot_read_or_move_a_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    worker = await _worker(db_session_factory, run, 0)
    stranger = (await _run(db_session_factory, key="op-s")).tenant_id
    with pytest.raises(FleetConflict):
        await fleet.run_status(db_session_factory, tenant_id=stranger, run_id=run.id)
    assert (
        await fleet.claim_worker(db_session_factory, tenant_id=stranger, run_id=run.id, cap=3)
        is None
    )
    with pytest.raises(FleetConflict):
        await fleet.finish(
            db_session_factory,
            tenant_id=stranger,
            worker_id=worker.id,
            expected_version=1,
            status="cancelled",
            error_code="x",
        )


async def test_the_sweep_finds_workers_past_their_deadline_or_without_a_live_lease(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    for ordinal in range(3):
        await _worker(db_session_factory, run, ordinal)
    sm, tenant = db_session_factory, run.tenant_id
    leased, unleased, other = [
        await fleet.claim_worker(sm, tenant_id=tenant, run_id=run.id, cap=3) for _ in range(3)
    ]
    assert leased and unleased and other
    store = PostgresStateStore(sm, trust_caller_clock=True)
    clock = datetime.now(UTC)
    await store.acquire_lease(
        worker_slot(run, leased), holder="p", turn_id="t", now=clock, ttl=timedelta(minutes=5)
    )

    # Within the grace after a claim, nothing is stale yet.
    assert await fleet.stale_workers(sm, now=clock, grace=timedelta(minutes=1)) == []

    later = clock + timedelta(minutes=2)
    stale = await fleet.stale_workers(sm, now=later, grace=timedelta(minutes=1))
    assert {worker.id for _, worker in stale} == {unleased.id, other.id}  # leased one is alive

    past_deadline = LATER + timedelta(seconds=1)
    every = await fleet.stale_workers(sm, now=past_deadline, grace=timedelta(minutes=1))
    assert {worker.id for _, worker in every} == {leased.id, unleased.id, other.id}
    async with sm() as session:
        assert await mux_state.active_lease(session, worker_slot(run, leased)) is not None


async def test_a_worker_or_run_past_its_deadline_is_not_started(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _run(db_session_factory)
    await _worker(db_session_factory, run, 0)
    async with db_session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE fleet_worker SET deadline_at = now() - interval '1 second' WHERE run_id = :r"
            ),
            {"r": run.id},
        )
    assert (
        await fleet.claim_worker(db_session_factory, tenant_id=run.tenant_id, run_id=run.id, cap=3)
        is None
    )
    late = await _run(db_session_factory, key="op-late")
    await _worker(db_session_factory, late, 0)
    async with db_session_factory.begin() as session:
        await session.execute(
            text("UPDATE fleet_run SET deadline_at = now() - interval '1 second' WHERE id = :r"),
            {"r": late.id},
        )
    assert (
        await fleet.claim_worker(
            db_session_factory, tenant_id=late.tenant_id, run_id=late.id, cap=3
        )
        is None
    )
