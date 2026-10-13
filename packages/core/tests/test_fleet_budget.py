"""Owned PostgreSQL product admission/exposure, with mock tariffs and no SDK I/O."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.authz import AgentRef, FleetFacts, Place, Subject
from daimon.core.channel_budget import (
    admit_fleet_worker,
    produce_fleet_budget,
    watch_fleet_budget,
)
from daimon.core.fleet import FleetConflict, worker_slot
from daimon.core.neutral_resources import WorkerLimits, WorkerPermissions, resolve_worker_spec
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import (
    channel_budgets,
    fleet,
    mux_state,
    security_audit,
    tenant_ledger,
    tenants,
)
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    NetworkPolicy,
    ProviderBinding,
    ToolSpec,
)
from mux.contracts.usage import UsageObservation
from mux.profiles import backend_selection, get_profile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

POLICY = TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini"))


def root(provider="gemini", *, budget="10"):
    now = datetime.now(UTC)
    selection = backend_selection(provider)
    model = ModelRef(provider=provider, id=selection.model or "claude-haiku-5-5")
    scope = Scope(
        tenant_id=str(uuid4()),
        account_id=str(uuid4()),
        principal_id="host",
        authorization_id="owned-origin",
    )
    thread = ThreadRef(
        channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="channel"),
        thread_id="root-thread",
    )
    profile = get_profile(selection.profile)
    spec = AgentSpec(
        name="root",
        model=model,
        system="Protected root instruction",
        tools=(ToolSpec(name=profile.execution_tool, kind="builtin"),)
        if profile.implicit_execution
        else (),
    )
    return WorkerPermissions(
        fleet=FleetFacts(
            scope=scope,
            root=thread,
            origin_id=uuid4(),
            account_id=UUID(scope.account_id),
            run_id=uuid4(),
            place=Place(channel_id=thread.thread_id, parent_channel_id=thread.channel.channel_id),
            enabled=True,
            active=True,
        ),
        subject=Subject(platform_user_id="tester"),
        agent=AgentRef.of("root"),
        spec=spec,
        base_system=spec.system,
        backend_grants=(selection,),
        models=(model,),
        limits=WorkerLimits(
            budget_usd=Decimal(budget), deadline=now + timedelta(minutes=10), max_turns=4
        ),
        environment=EnvironmentSpec(
            name="owned-box",
            network=NetworkPolicy(mode="limited", allowed_hosts=("offline.invalid",)),
        ),
        agent_id=uuid4(),
    )


def tariffs(parent):
    model = parent.spec.model
    return {
        (model.provider, model.id): ModelRates(2, 10, 3, 1)
        if model.provider == "anthropic"
        else ProviderPrice(
            model.provider,
            model.id,
            datetime.now(UTC).date(),
            Decimal(2),
            Decimal(10),
            Decimal(1),
            Decimal(3),
        )
    }


@pytest_asyncio.fixture
async def sm(db_preflight, db_engine, db_clean):
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def setup(sm, *, provider="gemini", funds="10", cap="3", parent=None):
    parent = parent or root(provider)
    async with sm() as db, db.begin():
        tenant = await make_tenant(db, id=UUID(parent.fleet.scope.tenant_id))
        await make_account(db, id=parent.fleet.account_id, tenant=tenant)
        await tenants.set_funding_mode(db, tenant_id=tenant.id, funding_mode="prepaid")
        await tenant_ledger.insert_entry(
            db,
            tenant_id=tenant.id,
            delta_usd=Decimal(funds),
            reason="trial_credit",
            idempotency_key=f"trial:{tenant.id}",
        )
        await mux_state.put_binding(
            db,
            ProviderBinding(
                id="root-binding",
                thread=parent.fleet.root,
                legacy_account_id=str(parent.fleet.account_id),
                provider=parent.spec.model.provider,
                profile=parent.backend_grants[0].profile,
                native_refs={"session": "root-session"},
                generation=1,
                config_revision=0,
            ),
            expected_generation=0,
        )
        run = await fleet.create_run(
            db,
            tenant_id=tenant.id,
            orchestrator_account_id=parent.fleet.account_id,
            orchestrator_session_id="root-session",
            orchestrator_binding_id="root-binding",
            platform="discord",
            channel_id="channel",
            thread_id="root-thread",
            operation_key="owned-spawn",
            total_budget_usd=Decimal(cap),
            deadline_at=parent.limits.deadline,
            max_workers=8,
            created_by="host",
        )
    parent = replace(parent, fleet=replace(parent.fleet, run_id=run.id))
    return parent, run


async def add(sm, parent, *, ordinal=0, budget="1"):
    limits = parent.limits.model_copy(update={"budget_usd": Decimal(budget)})
    resolved = resolve_worker_spec(
        parent, parent.spec, policy=POLICY, now=datetime.now(UTC), limits=limits
    )
    async with sm() as db, db.begin():
        return await fleet.add_worker(
            db,
            tenant_id=UUID(parent.fleet.scope.tenant_id),
            run_id=parent.fleet.run_id,
            ordinal=ordinal,
            role="worker",
            spec=resolved.spec.model_dump(mode="json"),
            spec_digest=resolved.digest,
            backend=resolved.backend.backend,
            profile=resolved.backend.profile,
            model=resolved.spec.model.id,
            budget_usd=Decimal(budget),
            deadline_at=limits.deadline,
            max_turns=limits.max_turns,
        )


async def next_run(sm, parent, run, *, cap="2"):
    async with sm() as db, db.begin():
        other = await fleet.create_run(
            db,
            tenant_id=run.tenant_id,
            orchestrator_account_id=parent.fleet.account_id,
            orchestrator_session_id=run.orchestrator_session_id,
            orchestrator_binding_id=run.orchestrator_binding_id,
            platform=run.platform,
            channel_id=run.channel_id,
            thread_id=run.thread_id,
            operation_key=f"next:{uuid4()}",
            total_budget_usd=Decimal(cap),
            deadline_at=run.deadline_at,
            max_workers=4,
            created_by="host",
        )
    return replace(parent, fleet=replace(parent.fleet, run_id=other.id)), other


async def reserve(sm, parent, worker, *, session=None, prices=None):
    with localcontext() as context:
        context.prec = 80
        budget = worker.budget_usd.normalize()
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": budget}),
    )

    async def admit(db):
        return await admit_fleet_worker(
            db,
            parent=parent,
            policy=POLICY,
            resolved=resolved,
            ordinal=worker.ordinal,
            role=worker.role,
            prices=prices if prices is not None else tariffs(parent),
            markup=Decimal(1),
        )

    if session is not None:
        return await admit(session)
    async with sm() as db, db.begin():
        return await admit(db)


async def read(sm, parent, worker, *, prices=None):
    async with sm() as db, db.begin():
        return await produce_fleet_budget(
            db,
            parent=parent,
            worker_id=worker.id,
            prices=prices if prices is not None else tariffs(parent),
            markup=Decimal(1),
        )


async def usage(sm, parent, run, worker, *, inputs=100, outputs=None, revision=1):
    provider = parent.spec.model.provider
    binding = ProviderBinding(
        id=f"worker-{worker.id}",
        thread=worker_slot(run, worker).thread,
        legacy_account_id=str(parent.fleet.account_id),
        provider=provider,
        profile=worker.profile,
        native_refs={"session": f"native-{worker.id}"},
        generation=1,
        config_revision=0,
    )
    native = ResourceRef(
        id=binding.native_refs["session"],
        kind="session",
        provider=provider,
        account_scope_id="owned-workspace",
        tenant_id=str(run.tenant_id),
        account_id=str(run.orchestrator_account_id),
    )
    value = UsageObservation(
        id="original-meter",
        session=native,
        revision=revision,
        model=parent.spec.model,
        turn_id="original-turn",
        grain="model_request" if provider == "anthropic" else "turn",
        basis="cumulative",
        input_tokens=inputs,
        input_cached_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=outputs,
        observed_at=datetime.now(UTC),
        completeness="partial" if outputs is None else "measured",
        native_meter={"offline": True},
    )
    async with sm() as db, db.begin():
        if revision == 1:
            await mux_state.put_binding(db, binding, expected_generation=0)
        await record_observation_usage(
            db,
            binding_id=binding.id,
            observation=value,
            tenant_id=run.tenant_id,
            platform_user_id="tester",
            pricing=ModelRates(2, 10, 3, 1),
            provider_price=tariffs(parent)[(provider, parent.spec.model.id)]
            if provider != "anthropic"
            else None,
            infrastructure_usd=None,
            billing_grain=value.grain,
            channel_id=run.channel_id,
        )
    return value


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_real_reservation_is_idempotent_and_unknown_consumes_bound_after_restart(
    sm, provider
):
    parent, run = await setup(sm, provider=provider)
    worker = await add(sm, parent)
    first = await reserve(sm, parent, worker)
    again = await reserve(sm, parent, worker)
    assert first.admitted and again.admitted
    assert first.worker_exposure_usd == again.worker_exposure_usd == Decimal(1)
    assert first.run_exposure_usd == again.run_exposure_usd == Decimal(1)
    await usage(sm, parent, run, worker, inputs=100, outputs=None)
    # Independent factory/controller lifetime; no retained Python budget cache.
    restarted = async_sessionmaker(sm.kw["bind"], expire_on_commit=False)
    current = await read(restarted, parent, worker)
    assert current.worker_exposure_usd == Decimal(1)
    assert current.worker_floor_usd >= Decimal(".0002")
    assert current.run_exposure_usd == Decimal(1)
    async with sm() as db:
        audits = await security_audit.list_events(db, tenant_id=run.tenant_id)
        ledger = await tenant_ledger.list_for_tenant(db, tenant_id=run.tenant_id)
    assert len(audits) == 1
    assert [entry.reason for entry in ledger] == (
        ["trial_credit", "turn_debit"] if provider == "anthropic" else ["trial_credit"]
    )


async def test_concurrent_spawns_cannot_spend_same_run_room(sm):
    parent, _ = await setup(sm, cap="1")
    a = await add(sm, parent, budget=".6")
    b = await add(sm, parent, ordinal=1, budget=".6")
    results = await asyncio.gather(reserve(sm, parent, a), reserve(sm, parent, b))
    assert sum(value.admitted for value in results) == 1
    current = await read(sm, parent, a)
    assert current.run_exposure_usd == Decimal(".6")


async def test_prepaid_tenant_cap_and_authenticated_parent_cap_refuse_spawn(sm):
    parent, _ = await setup(sm, funds=".5", cap="3")
    worker = await add(sm, parent)
    refused = await reserve(sm, parent, worker)
    assert not refused.admitted and refused.reason == "tenant_budget_exceeded"
    assert refused.worker_exposure_usd == 0
    narrow = replace(parent, limits=parent.limits.model_copy(update={"budget_usd": Decimal(".2")}))
    with pytest.raises((FleetConflict, ValueError)):
        await reserve(sm, narrow, worker)


async def test_real_usage_overrun_persists_and_refuses_new_spawn(sm):
    parent, run = await setup(sm, cap="1")
    a = await add(sm, parent, budget=".5")
    b = await add(sm, parent, ordinal=1, budget=".1")
    assert (await reserve(sm, parent, a)).admitted
    await usage(sm, parent, run, a, inputs=1_000_000, outputs=None)
    current = await read(sm, parent, a)
    assert current.worker_floor_usd == Decimal(2)
    assert current.worker_exposure_usd == current.run_exposure_usd == Decimal(2)
    refused = await reserve(sm, parent, b)
    assert not refused.admitted and refused.reason == "run_budget_exceeded"
    assert refused.worker_exposure_usd == 0 and refused.run_exposure_usd == 2


async def test_watchdog_uses_product_usage_and_retains_hold_on_cancel(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent, budget=".01")
    assert (await reserve(sm, parent, worker)).admitted
    cancelled, finished = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled=cancelled,
            finished=finished,
            poll_interval=0.01,
            read_timeout=3,
        )
    )
    await usage(sm, parent, run, worker, inputs=10_000, outputs=None)
    reason = await asyncio.wait_for(task, 5)
    assert reason == "budget_exceeded" and cancelled.is_set()
    current = await read(sm, parent, worker)
    assert current.worker_exposure_usd == Decimal(".02")


async def test_low_decimal_precision_does_not_round_reservations_or_run_sums(sm):
    parent, _ = await setup(sm, cap="2.45")
    a = await add(sm, parent, budget="1.2345")
    b = await add(sm, parent, ordinal=1, budget="1.23")
    with localcontext() as context:
        context.prec = 2
        first = await reserve(sm, parent, a)
        refused = await reserve(sm, parent, b)
    assert first.admitted and first.run_exposure_usd == Decimal("1.2345")
    assert not refused.admitted
    assert refused.run_exposure_usd == Decimal("1.2345")


async def test_zero_native_usage_and_terminal_status_cannot_free_unknown_hold(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    funded = await reserve(sm, parent, worker)
    assert funded.admitted
    await usage(sm, parent, run, worker, inputs=None, outputs=None)
    current = await read(sm, parent, worker)
    assert current.worker_floor_usd == 0 and current.worker_exposure_usd == 1
    async with sm() as db, db.begin():
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=current.worker.version,
            status="cancelled",
            error_code=None,
        )
    after = await read(sm, parent, worker)
    assert after.reason == "terminal" and after.worker_exposure_usd == 1
    assert after.prepaid_available_usd == Decimal(9)


async def test_active_channel_budget_refuses_total_run_bound(sm):
    parent, run = await setup(sm, cap="2")
    worker = await add(sm, parent, budget=".1")
    async with sm() as db, db.begin():
        await channel_budgets.set_channel_budget(
            db,
            tenant_id=run.tenant_id,
            platform=run.platform,
            channel_id=run.channel_id,
            limit_usd=Decimal(".5"),
            window="total",
            starts_at=None,
            ends_at=None,
            set_by_account_id=parent.fleet.account_id,
        )
    snapshot = await reserve(sm, parent, worker)
    assert not snapshot.admitted and snapshot.reason == "channel_budget_exceeded"
    assert snapshot.worker_exposure_usd == 0


async def test_new_run_counts_unknown_holds_from_other_completed_runs(sm):
    parent, run = await setup(sm, funds="3", cap="3")
    a = await add(sm, parent, budget="2")
    funded = await reserve(sm, parent, a)
    assert funded.admitted
    async with sm() as db, db.begin():
        other = await fleet.create_run(
            db,
            tenant_id=run.tenant_id,
            orchestrator_account_id=parent.fleet.account_id,
            orchestrator_session_id=run.orchestrator_session_id,
            orchestrator_binding_id=run.orchestrator_binding_id,
            platform=run.platform,
            channel_id=run.channel_id,
            thread_id=run.thread_id,
            operation_key="other-root-operation",
            total_budget_usd=Decimal(2),
            deadline_at=run.deadline_at,
            max_workers=4,
            created_by="host",
        )
    other_parent = replace(parent, fleet=replace(parent.fleet, run_id=other.id))
    b = await add(sm, other_parent, budget=".1")
    refused = await reserve(sm, other_parent, b)
    assert not refused.admitted and refused.reason == "tenant_budget_exceeded"
    assert refused.worker_exposure_usd == 0


async def test_unknown_pricing_and_erased_actor_cannot_authorize_dispatch(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    refused = await reserve(sm, parent, worker, prices={})
    assert not refused.admitted and refused.reason == "pricing_unavailable"
    assert (await read(sm, parent, worker)).worker_exposure_usd == 0
    worker = await add(sm, parent, ordinal=1)
    assert (await reserve(sm, parent, worker)).admitted
    from daimon.core.stores import accounts

    async with sm() as db, db.begin():
        await accounts.delete_account(db, account_id=parent.fleet.account_id)
    with pytest.raises(FleetConflict, match="live tenant actor"):
        await read(sm, parent, worker)


async def test_unfunded_pool_claim_is_still_refused_by_product_money_gate(sm):
    parent, _ = await setup(sm, funds=".1", cap="3")
    worker = await add(sm, parent)
    async with sm() as db, db.begin():
        claimed = await fleet.claim_worker(
            db, tenant_id=UUID(parent.fleet.scope.tenant_id), run_id=parent.fleet.run_id, cap=3
        )
    assert claimed.id == worker.id
    refused = await reserve(sm, parent, claimed)
    assert not refused.admitted and refused.worker_exposure_usd == 0
    # A claimed pool slot carries no financial admission or provider call.


async def test_read_unavailable_logs_and_cancels_without_releasing_hold(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=100, outputs=None)
    cancelled = asyncio.Event()
    reason = await watch_fleet_budget(
        sessionmaker=sm,
        parent=parent,
        worker_id=uuid4(),
        prices=tariffs(parent),
        markup=Decimal(1),
        cancelled=cancelled,
        finished=asyncio.Event(),
        poll_interval=0.01,
        read_timeout=3,
    )
    assert reason == "usage_unavailable" and cancelled.is_set()
    assert (await read(sm, parent, worker)).worker_exposure_usd == 1


async def test_equal_fully_reserved_run_stays_live_until_driver_finished(sm):
    parent, _ = await setup(sm, cap="1")
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    finished, cancelled = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled=cancelled,
            finished=finished,
            poll_interval=0.01,
            read_timeout=3,
        )
    )
    await asyncio.sleep(0.05)
    assert not cancelled.is_set()
    finished.set()
    assert await asyncio.wait_for(task, 3) == "finished"
    assert (await read(sm, parent, worker)).worker_exposure_usd == 1


async def test_customer_debit_is_not_added_again_to_its_held_exposure(sm):
    parent, run = await setup(sm, provider="anthropic")
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=100_000, outputs=100)
    current = await read(sm, parent, worker)
    assert current.run.observed_usd == Decimal(".201")
    assert current.worker_exposure_usd == 1
    assert current.prepaid_available_usd == 9


async def test_preallocation_gate_refuses_unfunded_claim_then_accepts_real_reservation(sm):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, _ = await setup(sm)
    worker = await add(sm, parent)
    async with sm() as db, db.begin():
        claimed = await fleet.claim_worker(
            db, tenant_id=UUID(parent.fleet.scope.tenant_id), run_id=parent.fleet.run_id, cap=3
        )
    async with sm() as db, db.begin():
        refused = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
        )
    assert not refused.admitted
    assert (await reserve(sm, parent, claimed)).admitted
    async with sm() as db, db.begin():
        accepted = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
        )
    assert accepted.admitted and accepted.worker_exposure_usd == 1


@pytest.mark.parametrize("price_state", ["missing", "unverified", "future", "valid"])
async def test_funded_unbound_preallocation_revalidates_tariff_and_keeps_hold(sm, price_state):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    async with sm() as db, db.begin():
        claimed = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert claimed is not None and claimed.binding_id is None
    key = (worker.backend, worker.model)
    prices = tariffs(parent)
    if price_state == "missing":
        prices = {}
    elif price_state == "unverified":
        prices[key] = replace(prices[key], verified=False)
    elif price_state == "future":
        prices[key] = replace(prices[key], checked_on=datetime.now(UTC).date() + timedelta(days=1))
    async with sm() as db, db.begin():
        snapshot = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=prices,
            markup=Decimal(1),
        )
    assert snapshot.admitted is (price_state == "valid")
    assert snapshot.reason == (None if price_state == "valid" else "pricing_unavailable")
    assert snapshot.worker_exposure_usd == snapshot.run_exposure_usd == 1
    assert (await read(sm, parent, worker)).worker_exposure_usd == 1


async def test_open_fleet_transaction_does_not_block_another_tenants_ordinary_ledger(sm):
    parent, _ = await setup(sm)
    worker = await add(sm, parent)
    async with sm() as db, db.begin():
        other = await make_tenant(db)
    async with sm() as admission, admission.begin():
        snapshot = await reserve(sm, parent, worker, session=admission)
        assert snapshot.admitted
        # The original append-only writer must commit while admission stays open.
        async with sm() as ordinary, ordinary.begin():
            await ordinary.execute(text("SET LOCAL lock_timeout = '3s'"))
            for amount, reason in ((Decimal("-.25"), "turn_debit"), (Decimal(".5"), "topup")):
                assert await tenant_ledger.insert_entry(
                    ordinary,
                    tenant_id=other.id,
                    delta_usd=amount,
                    reason=reason,
                    idempotency_key=f"ordinary:{reason}:{other.id}",
                )
        async with sm() as observer:
            assert await tenant_ledger.get_balance(observer, tenant_id=other.id) == Decimal(".25")


async def test_same_tenant_ordinary_debit_serializes_before_fleet_funds_check(sm):
    parent, run = await setup(sm, funds="1", cap="1")
    worker = await add(sm, parent)
    started = asyncio.Event()
    admission_pid = None

    async def compete():
        nonlocal admission_pid
        async with sm() as db, db.begin():
            admission_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            return await reserve(sm, parent, worker, session=db)

    async with sm() as ordinary:
        await ordinary.begin()
        ordinary_pid = await ordinary.scalar(text("SELECT pg_backend_pid()"))
        await tenant_ledger.insert_entry(
            ordinary,
            tenant_id=run.tenant_id,
            delta_usd=Decimal("-.75"),
            reason="turn_debit",
            idempotency_key=f"ordinary:{run.tenant_id}",
        )
        task = asyncio.create_task(compete())
        try:
            await asyncio.wait_for(started.wait(), 3)
            async with asyncio.timeout(3):
                while ordinary_pid not in await ordinary.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": admission_pid}
                ):
                    assert not task.done()
                    await asyncio.sleep(0.01)
            await ordinary.commit()
            snapshot = await asyncio.wait_for(task, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not snapshot.admitted and snapshot.reason == "tenant_budget_exceeded"
    assert snapshot.worker_exposure_usd == 0
    assert snapshot.prepaid_available_usd == Decimal(".25")


async def test_flag_off_financial_admission_does_not_touch_database():
    parent = root()
    disabled = replace(parent, fleet=replace(parent.fleet, enabled=False))

    class NoDB:
        def __getattr__(self, name):
            raise AssertionError("flag-off admission must not read or allocate")

    with pytest.raises(FleetConflict, match="root authorization"):
        await admit_fleet_worker(
            NoDB(),
            parent=disabled,
            policy=POLICY,
            resolved=resolve_worker_spec(parent, parent.spec, policy=POLICY, now=datetime.now(UTC)),
            ordinal=0,
            role="worker",
            prices=tariffs(parent),
            markup=Decimal(1),
        )


@pytest.mark.parametrize("fault", ["foreign_account", "foreign_run", "root_binding", "no_audit"])
async def test_root_scope_or_missing_actor_audit_blocks_allocation(sm, fault):
    parent, _ = await setup(sm)
    worker = await add(sm, parent)
    if fault == "foreign_account":
        parent = replace(parent, fleet=replace(parent.fleet, account_id=uuid4()))
    elif fault == "foreign_run":
        parent = replace(parent, fleet=replace(parent.fleet, run_id=uuid4()))
    elif fault == "root_binding":
        parent = replace(
            parent,
            fleet=replace(
                parent.fleet, root=parent.fleet.root.model_copy(update={"thread_id": "foreign"})
            ),
        )
    else:
        parent = replace(parent, agent_id=None)
    with pytest.raises((FleetConflict, ValueError)):
        await reserve(sm, parent, worker)


async def test_monetary_producer_retains_floor_even_when_a_sibling_is_unpriced(sm):
    parent, run = await setup(sm, provider="anthropic", cap="1")
    worker = await add(sm, parent, budget=".5")
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=1_000_000, outputs=1)
    current = await read(sm, parent, worker, prices={})
    assert current.worker_exposure_usd >= Decimal(2)
    assert not current.admitted


async def test_old_native_attempt_cannot_reopen_financial_admission(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    funded = await reserve(sm, parent, worker)
    async with sm() as db, db.begin():
        claimed = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
        await fleet.requeue(db, tenant_id=run.tenant_id, worker=claimed, max_attempts=2)
    async with sm() as db, db.begin():
        _, workers = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
    refused = await reserve(sm, parent, workers[0])
    assert not refused.admitted and refused.reason == "accepted_attempt_inventory_unavailable"
    assert refused.worker_exposure_usd == funded.worker_exposure_usd == 1


async def test_never_claimed_cancel_releases_zero_spend_and_next_run_can_use_funds(sm):
    parent, run = await setup(sm, funds="3", cap="3")
    worker = await add(sm, parent, budget="2")
    funded = await reserve(sm, parent, worker)
    async with sm() as db, db.begin():
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=funded.worker.version,
            status="cancelled",
            error_code="cancelled_before_claim",
        )
        await fleet.set_run_status(
            db,
            tenant_id=run.tenant_id,
            run_id=run.id,
            expected_version=funded.run.version,
            status="cancelled",
        )
    other_parent, other = await next_run(sm, parent, run)
    other_worker = await add(sm, other_parent, budget="2")
    accepted = await reserve(sm, other_parent, other_worker)
    assert accepted.admitted and accepted.prepaid_available_usd == 1
    released = await read(sm, parent, worker)
    assert released.worker_exposure_usd == released.run_exposure_usd == 0
    assert released.reason == "terminal"
    async with sm() as db, db.begin():
        graph, stamps = await fleet.lock_budget(
            db, tenant_id=run.tenant_id, actor_id=parent.fleet.account_id, run_id=other.id
        )
        assert run.id not in {item.id for item, _ in graph}
        assert worker.id not in stamps
        ledger = await tenant_ledger.list_for_tenant(db, tenant_id=run.tenant_id)
    assert len(ledger) == 1 and ledger[0].delta_usd == 3
    cancelled = asyncio.Event()
    assert (
        await watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled=cancelled,
            finished=asyncio.Event(),
            read_timeout=3,
        )
        == "terminal"
    )
    assert cancelled.is_set()


@pytest.mark.parametrize("evidence", ["claim", "requeue", "zero_revision"])
async def test_ambiguous_cancel_without_bound_binding_keeps_its_whole_hold(sm, evidence):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    funded = await reserve(sm, parent, worker)
    current = funded.worker
    if evidence in ("claim", "requeue"):
        async with sm() as db, db.begin():
            current = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
            if evidence == "requeue":
                current = await fleet.requeue(
                    db, tenant_id=run.tenant_id, worker=current, max_attempts=2
                )
    else:
        await usage(sm, parent, run, worker, inputs=0, outputs=0)
    assert current.binding_id is None
    async with sm() as db, db.begin():
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=current.version,
            status="cancelled",
            error_code="ambiguous_allocation",
        )
    after = await read(sm, parent, worker)
    assert after.reason == "terminal" and after.worker_exposure_usd == 1
    assert after.prepaid_available_usd == 9


async def test_another_runs_verified_overrun_does_not_latch_current_run(sm):
    parent, run = await setup(sm, provider="anthropic", funds="10", cap="1")
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=1_000_000, outputs=1)
    overrun = await read(sm, parent, worker)
    assert overrun.reason == "run_budget_exceeded"
    other_parent, _ = await next_run(sm, parent, run, cap="1")
    other_worker = await add(sm, other_parent, budget=".1")
    current = await reserve(sm, other_parent, other_worker)
    assert current.admitted and current.reason is None
    assert current.prepaid_available_usd == Decimal("7.899990")
    assert (await read(sm, parent, worker)).reason == "run_budget_exceeded"


async def test_financially_refused_spawn_is_terminal_and_cannot_claim_pool_slot(sm):
    parent, run = await setup(sm, funds=".1", cap="3")
    worker = await add(sm, parent)
    refused = await reserve(sm, parent, worker)
    assert not refused.admitted and refused.worker.status == "cancelled"
    async with sm() as db, db.begin():
        assert await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3) is None


async def test_terminal_fully_debited_history_is_not_in_the_next_budget_graph(sm):
    parent, run = await setup(sm, provider="anthropic", funds="3", cap="2")
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=500_000, outputs=0)
    spent = await read(sm, parent, worker)
    assert spent.worker_exposure_usd == spent.run.observed_usd == 1
    async with sm() as db, db.begin():
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=spent.worker.version,
            status="cancelled",
            error_code="terminal",
        )
        await fleet.set_run_status(
            db,
            tenant_id=run.tenant_id,
            run_id=run.id,
            expected_version=spent.run.version,
            status="cancelled",
        )
    other_parent, other = await next_run(sm, parent, run, cap="1")
    other_worker = await add(sm, other_parent)
    accepted = await reserve(sm, other_parent, other_worker)
    assert accepted.admitted and accepted.prepaid_available_usd == 1
    async with sm() as db, db.begin():
        graph, _ = await fleet.lock_budget(
            db, tenant_id=run.tenant_id, actor_id=parent.fleet.account_id, run_id=other.id
        )
        assert run.id not in {item.id for item, _ in graph}


async def test_store_cannot_lower_a_hold_without_the_producers_zero_spend_evidence(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    async with sm() as db, db.begin():
        graph, _ = await fleet.lock_budget(
            db, tenant_id=run.tenant_id, actor_id=parent.fleet.account_id, run_id=run.id
        )
        with pytest.raises(FleetConflict, match="cannot be released"):
            await fleet.save_budget(
                db,
                tenant_id=run.tenant_id,
                graph=graph,
                exposures={worker.id: Decimal(0)},
                debits={run.id: Decimal(0)},
            )
    assert (await read(sm, parent, worker)).worker_exposure_usd == 1


async def test_unrelated_open_ordinary_write_does_not_cancel_actual_budget_watcher(sm, monkeypatch):
    from daimon.core import channel_budget

    parent, _ = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    async with sm() as db, db.begin():
        other = await make_tenant(db)
    finished, cancelled, polled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = channel_budget.produce_fleet_budget

    async def observed_read(*args, **kwargs):
        value = await original(*args, **kwargs)
        assert value.admitted and value.worker_exposure_usd == 1
        polled.set()
        return value

    monkeypatch.setattr(channel_budget, "produce_fleet_budget", observed_read)
    async with sm() as ordinary, ordinary.begin():
        await tenant_ledger.insert_entry(
            ordinary,
            tenant_id=other.id,
            delta_usd=Decimal(1),
            reason="topup",
            idempotency_key=f"ordinary:{other.id}",
        )
        task = asyncio.create_task(
            watch_fleet_budget(
                sessionmaker=sm,
                parent=parent,
                worker_id=worker.id,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled=cancelled,
                finished=finished,
                read_timeout=3,
            )
        )
        try:
            await asyncio.wait_for(polled.wait(), 3)
            finished.set()
            assert await asyncio.wait_for(task, 3) == "finished"
            assert not cancelled.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_atomic_spawn_reservation_and_audit_roll_back_together(sm):
    from daimon.core.channel_budget import admit_fleet_worker

    parent, run = await setup(sm)
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": Decimal(1)}),
    )
    with pytest.raises(RuntimeError, match="crash"):
        async with sm() as db, db.begin():
            admitted = await admit_fleet_worker(
                db,
                parent=parent,
                policy=POLICY,
                resolved=resolved,
                ordinal=0,
                role="worker",
                prices=tariffs(parent),
                markup=Decimal(1),
            )
            assert admitted.admitted
            raise RuntimeError("crash before commit")
    async with sm() as db, db.begin():
        _, workers = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
        assert workers == ()
        assert await security_audit.list_events(db, tenant_id=run.tenant_id) == []
        admitted = await admit_fleet_worker(
            db,
            parent=parent,
            policy=POLICY,
            resolved=resolved,
            ordinal=0,
            role="worker",
            prices=tariffs(parent),
            markup=Decimal(1),
        )
    assert admitted.admitted and admitted.worker_exposure_usd == 1
