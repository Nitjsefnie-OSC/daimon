"""Run-scoped audit reads retain queue facts without authorizing a turn."""

from __future__ import annotations

import uuid
from datetime import timedelta

from daimon.core._models import TaskContinuation
from daimon.core.continuity.wakes import cancel_wake
from daimon.core.stores import fleet, task_continuations
from sqlalchemy import insert, text

from .test_run_end import _read, _waits
from .test_worker_finished import pooled as pooled


async def test_run_read_preserves_all_statuses_and_audit_in_read_only_transaction(pooled):
    run, initial, _ = await _waits(pooled)
    assert await cancel_wake(
        pooled, tenant_id=run.tenant_id, idempotency_key=initial[2].idempotency_key
    )
    expected = tuple(
        sorted(
            [await _read(pooled, row.idempotency_key) for row in initial],
            key=lambda row: (row.created_at, row.id),
        )
    )
    assert {row.status for row in expected} == {
        "pending",
        "claimed",
        "delivered",
        "skipped",
        "cancelled",
    }
    assert all(
        row.lease_owner is not None for row in expected if row.status in {"delivered", "skipped"}
    )
    async with pooled.begin() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        for _ in range(2):
            assert (
                await task_continuations.list_worker_finished_for_run(
                    db,
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                )
                == expected
            )
    assert [await _read(pooled, row.idempotency_key) for row in expected] == list(expected)
    actual, _ = await fleet.run_status(pooled, tenant_id=run.tenant_id, run_id=run.id)
    assert actual == run


async def test_scope_precedes_decode_and_requires_an_actual_owned_run(pooled, monkeypatch):
    run, owned, _ = await _waits(pooled)
    foreign, _, _ = await _waits(pooled)
    absent = uuid.uuid4()
    clone = owned[0].model_dump(exclude={"id"})
    async with pooled.begin() as db:
        # Reader-only adversarial fixtures, never valid frozen batch/authority proofs.
        for changes in (
            {
                "tenant_id": foreign.tenant_id,
                "requester_account_id": foreign.orchestrator_account_id,
            },
            {"requested_work": f'{{"run_id":"{absent}",broken'},
            {"reason": "timer"},
            {"requested_work": clone["requested_work"].replace('"run_id"', '"runXid"', 1)},
        ):
            await db.execute(
                insert(TaskContinuation).values(
                    **(clone | changes | {"idempotency_key": uuid.uuid4()}),
                )
            )
    expected = tuple(sorted(owned, key=lambda row: (row.created_at, row.id)))
    original = task_continuations.TaskContinuationRow.model_validate
    decoded = []

    def guarded(row, *args, **kwargs):
        assert row.id in {item.id for item in owned}
        decoded.append(row.id)
        return original(row, *args, **kwargs)

    monkeypatch.setattr(task_continuations.TaskContinuationRow, "model_validate", guarded)
    async with pooled() as db:
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=run.id
            )
            == expected
        )
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=foreign.tenant_id, run_id=run.id
            )
            == ()
        )
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=foreign.id
            )
            == ()
        )
        assert (
            await task_continuations.list_worker_finished_for_run(
                db, tenant_id=run.tenant_id, run_id=absent
            )
            == ()
        )
    assert decoded == [row.id for row in expected]


async def test_owned_malformed_body_and_bad_root_stay_visible_for_consumer_refusal(pooled):
    run, owned, _ = await _waits(pooled)
    clone = owned[0].model_dump(exclude={"id"})
    keys = [uuid.uuid4(), uuid.uuid4()]
    body = f'{{"run_id":"{run.id}",broken'
    async with pooled.begin() as db:
        for key, changes in zip(
            keys,
            (
                {"requested_work": body, "created_at": owned[0].created_at - timedelta(days=1)},
                {"thread_id": "other-root"},
            ),
            strict=True,
        ):
            await db.execute(
                insert(TaskContinuation).values(**(clone | changes | {"idempotency_key": key}))
            )
    async with pooled() as db:
        rows = await task_continuations.list_worker_finished_for_run(
            db, tenant_id=run.tenant_id, run_id=run.id
        )
    assert len(rows) == len(owned) + 2
    assert rows[0].idempotency_key == keys[0]
    assert next(row for row in rows if row.idempotency_key == keys[0]).requested_work == body
    assert next(row for row in rows if row.idempotency_key == keys[1]).thread_id == "other-root"
