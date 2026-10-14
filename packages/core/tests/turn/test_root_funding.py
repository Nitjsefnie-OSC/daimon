"""Original root reservations share the existing tenant financial fence."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from daimon.core.channel_budget import require_root_funding
from daimon.core.fleet import FleetConflict
from daimon.core.stores import mux_state, tenant_ledger, tenants
from mux.errors import OperationConflict, ScopeViolation
from mux.state.store import binding_slot

from .test_frozen_root import root_turn as root_turn


def controls(value):
    _, _, _, intent, origin, *_ = value
    return {
        "origin_id": str(origin.id),
        "intent_id": str(intent.id),
        "cash_budget_usd": "0.06",
        "native_budget_usd": "0.03",
        "max_inputs": 3,
    }


async def credit(value, amount="0.06"):
    deps, _, _, _, _, scope, *_ = value
    async with deps.sessionmaker.begin() as db:
        await tenants.set_funding_mode(db, tenant_id=UUID(scope.tenant_id), funding_mode="prepaid")
        await tenant_ledger.insert_entry(
            db,
            tenant_id=UUID(scope.tenant_id),
            delta_usd=Decimal(amount),
            reason="test",
            idempotency_key="root-test-credit:" + scope.tenant_id,
        )


async def funded(value, *, initialize=False, selected=None, slot=None):
    deps, _, _, intent, origin, scope, binding, _ = value
    async with deps.sessionmaker.begin() as db:
        return await require_root_funding(
            db,
            scope,
            slot or binding_slot(binding),
            origin_id=origin.id,
            controls=controls(value) if selected is None else selected,
            markup=Decimal(2),
            intent_id=intent.id if initialize else None,
        )


async def test_exact_funds_reserve_once_and_replay_original_unknown(root_turn):
    deps, _, _, _, origin, scope, binding, transport = root_turn
    await credit(root_turn)
    before = len(transport.requests)
    original = await funded(root_turn, initialize=True)
    assert original.result["root_finance"]["exposure_usd"] == "0.06"
    lease = await deps.state_store.acquire_lease(
        binding_slot(binding),
        holder="retained-root",
        turn_id="original",
        now=original.operation.created_at,
        ttl=timedelta(minutes=5),
    )
    try:
        await deps.state_store.claim_send(
            scope, original.operation.key, now=original.operation.created_at, fence=lease
        )
        await deps.state_store.advance_operation(
            scope,
            original.operation.key,
            "outcome_unknown",
            now=original.operation.created_at,
            fence=lease,
        )
    finally:
        await deps.state_store.release_lease(lease)
    replay = await funded(root_turn, initialize=True)
    recheck = await funded(root_turn)
    assert replay == recheck and replay.operation.status == "outcome_unknown"
    assert replay.result == original.result
    assert len(transport.requests) == before
    async with deps.sessionmaker() as db:
        assert await tenant_ledger.get_balance(db, tenant_id=UUID(scope.tenant_id)) == Decimal(
            "0.06"
        )
        roots = await mux_state.list_fleet_root_finances(db, tenant_id=UUID(scope.tenant_id))
        assert [r.operation.key for r in roots] == [f"fleet_root:{origin.id}:root:0"]


@pytest.mark.parametrize("fault", ["missing", "undersized", "controls", "slot", "exhausted"])
async def test_unproved_original_hold_refuses_without_input(root_turn, fault):
    deps, _, _, _, origin, scope, binding, transport = root_turn
    await credit(root_turn)
    selected, slot = controls(root_turn), binding_slot(binding)
    if fault != "missing":
        await funded(root_turn, initialize=True)
    if fault == "undersized":
        async with deps.sessionmaker.begin() as db:
            await mux_state.update_fleet_root_finance(
                db,
                scope,
                origin_id=origin.id,
                root_finance={
                    "exposure_usd": "0.01",
                    "debit_usd": "0",
                    "input_keys": [],
                    "input_outcomes": {},
                },
            )
    elif fault == "controls":
        selected["max_inputs"] = 4
    elif fault == "slot":
        slot = slot.model_copy(
            update={"thread": slot.thread.model_copy(update={"thread_id": "foreign"})}
        )
    elif fault == "exhausted":
        async with deps.sessionmaker.begin() as db:
            await tenant_ledger.insert_entry(
                db,
                tenant_id=UUID(scope.tenant_id),
                delta_usd=Decimal("-0.01"),
                reason="test",
                idempotency_key="root-other-debit:" + scope.tenant_id,
            )
    before = len(transport.requests)
    with pytest.raises(FleetConflict):
        await funded(root_turn, selected=selected, slot=slot)
    assert len(transport.requests) == before


async def test_concurrent_same_original_reservation_uses_one_hold(root_turn):
    deps, _, _, _, _, scope, _, transport = root_turn
    await credit(root_turn)
    before = len(transport.requests)
    first, second = await asyncio.gather(
        funded(root_turn, initialize=True), funded(root_turn, initialize=True)
    )
    assert first == second
    async with deps.sessionmaker() as db:
        roots = await mux_state.list_fleet_root_finances(db, tenant_id=UUID(scope.tenant_id))
        assert len(roots) == 1 and roots[0].result["root_finance"]["exposure_usd"] == "0.06"
    assert len(transport.requests) == before


async def test_store_mutable_finance_keeps_initial_digest_and_refuses_changed_controls(root_turn):
    deps, _, _, intent, origin, scope, binding, _ = root_turn
    await credit(root_turn)
    original = await funded(root_turn, initialize=True)
    changed = {
        **original.result["root_finance"],
        "input_keys": ["original-send"],
        "input_outcomes": {"original-send": None},
    }
    async with deps.sessionmaker.begin() as db:
        await mux_state.update_fleet_root_finance(
            db, scope, origin_id=origin.id, root_finance=changed
        )
    assert (await funded(root_turn, initialize=True)).result["root_finance"] == changed
    async with deps.sessionmaker.begin() as db:
        with pytest.raises(OperationConflict):
            await mux_state.get_fleet_root_operation(
                db,
                scope,
                intent_id=intent.id,
                origin_id=origin.id,
                slot=binding_slot(binding),
                request_digest=original.operation.request_digest,
                root_controls={**controls(root_turn), "max_inputs": 4},
                root_finance=changed,
            )
    async with deps.sessionmaker.begin() as db:
        with pytest.raises((ScopeViolation, KeyError)):
            await mux_state.update_fleet_root_finance(
                db,
                scope.model_copy(update={"account_id": str(UUID(int=123))}),
                origin_id=origin.id,
                root_finance=changed,
            )
    assert (await funded(root_turn)).result["root_finance"] == changed


async def test_cached_root_receipt_cannot_replace_current_controls_or_finance(root_turn):
    from datetime import timedelta

    deps, _, _, intent, origin, scope, binding, _ = root_turn
    await credit(root_turn)
    original = await funded(root_turn, initialize=True)
    lease = await deps.state_store.acquire_lease(
        binding_slot(binding),
        holder="root-receipt",
        turn_id="root-receipt",
        now=original.operation.created_at,
        ttl=timedelta(minutes=5),
    )
    try:
        await deps.state_store.claim_send(
            scope, original.operation.key, now=original.operation.created_at, fence=lease
        )
        await deps.state_store.advance_operation(
            scope,
            original.operation.key,
            "processed",
            now=original.operation.created_at,
            fence=lease,
            result={
                "intent_id": str(intent.id),
                "origin_id": str(origin.id),
                "root_controls": {"replaced": True},
                "root_finance": {"exposure_usd": "0"},
            },
        )
    finally:
        await deps.state_store.release_lease(lease)
    retained = await funded(root_turn)
    assert retained.result["root_controls"] == original.result["root_controls"]
    assert retained.result["root_finance"] == original.result["root_finance"]
    assert retained.operation.request_digest == original.operation.request_digest


async def test_financial_inventory_uses_literal_root_prefix(root_turn):
    from daimon.core._models import MuxOperation

    deps, _, _, _, _, scope, _, _ = root_turn
    await credit(root_turn)
    original = await funded(root_turn, initialize=True)
    key = original.operation.key.replace("fleet_root:", "fleetaroot:", 1)
    unrelated = original.model_copy(
        update={"operation": original.operation.model_copy(update={"key": key, "id": key})}
    )
    async with deps.sessionmaker.begin() as db:
        db.add(
            MuxOperation(
                tenant_id=UUID(scope.tenant_id),
                key=key,
                account_id=scope.account_id,
                principal_id=scope.principal_id,
                status=unrelated.operation.status,
                record=unrelated.model_dump(mode="json"),
                updated_at=unrelated.operation.updated_at,
            )
        )
    assert await funded(root_turn) == original
    async with deps.sessionmaker() as db:
        assert await mux_state.list_fleet_root_finances(db, tenant_id=UUID(scope.tenant_id)) == (
            original,
        )


async def test_dirty_financial_inventory_refuses_without_flushing(root_turn):
    from daimon.core._models import MuxOperation
    from daimon.core.stores import UnflushedReadState
    from sqlalchemy import select

    deps, _, _, _, _, scope, _, _ = root_turn
    await credit(root_turn)
    original = await funded(root_turn, initialize=True)
    async with deps.sessionmaker() as db:
        row = await db.scalar(
            select(MuxOperation).where(
                MuxOperation.tenant_id == UUID(scope.tenant_id),
                MuxOperation.key == original.operation.key,
            )
        )
        row.record = {
            **row.record,
            "result": {**row.record["result"], "root_finance": {"exposure_usd": "0"}},
        }
        with pytest.raises(UnflushedReadState):
            await mux_state.list_fleet_root_finances(db, tenant_id=UUID(scope.tenant_id))
        assert row in db.dirty
        await db.rollback()
    assert await funded(root_turn) == original


@pytest.mark.parametrize("path", ["mux", "legacy"])
async def test_budgeted_root_is_typed_refused_without_activation_or_native_effect(root_turn, path):
    from dataclasses import replace
    from datetime import UTC, datetime

    from daimon.core.authz import Subject
    from daimon.core.channel_backend import AgentBackendService, BackendActor
    from daimon.core.neutral_resources import WorkerSpecRefused
    from daimon.core.turn.prepare import bind_session_impl

    deps, prepared, _, intent, _, scope, _, transport = root_turn
    source = prepared._source
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    grant = await service.read_fleet_grant(source.tenant_id, source.agent_id, now=datetime.now(UTC))
    grant = grant.model_copy(
        update={"native_root_budget_usd": Decimal("0.03"), "root_cash_budget_usd": Decimal("0.06")}
    )
    preview = await service.preview_fleet_grant(source, grant, now=datetime.now(UTC))
    await service.set_fleet_grant(
        source,
        grant,
        preview.setting_revision,
        BackendActor(source.tenant_id, prepared.admission.account_id, Subject(is_admin=True)),
        expected_preview_digest=preview.digest,
        now=datetime.now(UTC),
    )
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused) as denied:
        await bind_session_impl(
            replace(deps, turn_path=path),
            prepared.admission,
            tenant_id=source.tenant_id,
            platform="discord",
            external_user_id="requester",
            thread_id=intent.thread_id,
            session_account_id=prepared.session_account_id,
            reuse_existing=False,
            turn_card_intent_id=intent.id,
        )
    assert denied.value.field == "native_budget_usd"
    assert len(transport.requests) == before
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) is None
        assert await mux_state.list_fleet_root_finances(db, tenant_id=UUID(scope.tenant_id)) == ()


@pytest.mark.parametrize(
    "fault", ["foreign_tenant", "foreign_account", "nan", "negative", "debit_above_exposure"]
)
async def test_original_inventory_refuses_foreign_or_incomplete_cash(root_turn, fault):
    from daimon.core._models import MuxOperation
    from sqlalchemy import update

    deps, _, _, _, _, scope, _, transport = root_turn
    await credit(root_turn)
    original = await funded(root_turn, initialize=True)
    slot, finance = original.slot, original.result["root_finance"]
    if fault == "foreign_tenant":
        slot = slot.model_copy(
            update={
                "thread": slot.thread.model_copy(
                    update={
                        "channel": slot.thread.channel.model_copy(
                            update={"tenant_id": str(UUID(int=123))}
                        )
                    }
                )
            }
        )
    elif fault == "foreign_account":
        slot = slot.model_copy(update={"account_id": str(UUID(int=123))})
    elif fault == "nan":
        finance = {**finance, "exposure_usd": "NaN"}
    elif fault == "negative":
        finance = {**finance, "debit_usd": "-0.01"}
    else:
        finance = {**finance, "debit_usd": "0.07"}
    changed = original.model_copy(
        update={"slot": slot, "result": {**original.result, "root_finance": finance}}
    )
    async with deps.sessionmaker.begin() as db:
        await db.execute(
            update(MuxOperation)
            .where(
                MuxOperation.tenant_id == UUID(scope.tenant_id),
                MuxOperation.key == original.operation.key,
            )
            .values(record=changed.model_dump(mode="json"))
        )
    before = len(transport.requests)
    with pytest.raises(FleetConflict):
        await funded(root_turn)
    assert len(transport.requests) == before
