"""Concrete fleet result/contract reads over real SDK worker journals."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core._models import FleetWorker as WorkerRow
from daimon.core.fleet import result_digest
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused, HostFleetPorts
from daimon.core.headless_runner import run_worker
from daimon.core.neutral_resources import resolve_worker_spec
from daimon.core.stores import access_policy, fleet
from mux.contracts.events import AgentMessagePayload, Event, NativeProvenance, TextPart
from mux.errors import OperationConflict
from sqlalchemy import update

from ..test_fleet_budget import POLICY, setup, tariffs
from ..test_fleet_budget import sm as sm
from .test_openai_worker_plan import application as application
from .test_openai_worker_plan import claimed_openai_worker as claimed_openai_worker
from .test_openai_worker_plan import db_preflight as db_preflight
from .test_openai_worker_plan import db_session_factory as db_session_factory
from .test_openai_worker_plan import owned as owned
from .test_openai_worker_plan import owned_database_preflight as owned_database_preflight
from .test_openai_worker_plan import provider as provider


async def test_real_worker_journal_collect_refuses_absent_required_review(
    claimed_openai_worker, monkeypatch
):
    request, root, worker, prices, _, _, wire = claimed_openai_worker
    native_handle = wire.handle
    full_text = "authoritative input " * 300

    def handle(call):
        response = native_handle(call)
        if call.method == "POST" and call.url.path.endswith("/events"):
            wire.items[-1]["content"][0]["text"] = full_text
        return response

    monkeypatch.setattr(wire, "handle", handle)
    # Configure the stored result policy before execution; native output, result
    # publication, private binding and financial admission remain product paths.
    async with request.deps.sessionmaker.begin() as db:
        await db.execute(
            update(WorkerRow)
            .where(WorkerRow.id == worker.id)
            .values(result_contract={"format": "text", "review_required": True})
        )
    worker = replace(worker, result_contract={"format": "text", "review_required": True})
    root = replace(
        root, subject=replace(root.subject, platform_user_id=root.fleet.scope.principal_id)
    )
    request = replace(
        request,
        admission=replace(
            request.admission, grant=replace(request.admission.grant, subject=root.subject)
        ),
    )
    result = await run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Complete the task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded"
    ports = HostFleetPorts(request.deps.sessionmaker, prices=prices, markup=Decimal(1))
    orchestrator = FleetOrchestrator(ports)
    run, _ = await ports.read_run(root.fleet.scope, worker.run_id)
    async with request.deps.sessionmaker() as db:
        policy = await access_policy.load_access_policy(db, tenant_id=request.tenant_id)
    recorded = await ports.read_result(root, policy, run, result)
    assert ports.validate_contract(result.result_contract, recorded) == full_text
    assert result.result.excerpt == full_text[:4096]
    assert len(full_text) > len(result.result.excerpt)
    assert result.result.digest == result_digest(dict(recorded.payload))
    assert await orchestrator.collect(root, policy, run_id=run.id) == {
        "run_id": str(run.id),
        "status": "blocked",
        "reason": "review_required",
    }
    with pytest.raises(FleetRefused, match="result_unavailable"):
        await ports.read_result(
            root, policy, run, replace(result, result=replace(result.result, digest="wrong"))
        )
    with pytest.raises(FleetRefused, match="foreign_run"):
        await ports.read_run(root.fleet.scope.model_copy(update={"account_id": "foreign"}), run.id)


@pytest.mark.parametrize(
    "format_,text", [("json", "NaN"), ("json", "1e999"), ("json", '{"a":1,"a":2}')]
)
def test_contract_validator_refuses_noncanonical_json(format_, text):
    ports = HostFleetPorts(None, prices={}, markup=Decimal(1))
    event = Event(
        id="result",
        session_id="session",
        sequence=0,
        type="agent.message",
        observed_at=datetime.now(UTC),
        authority="record",
        payload=AgentMessagePayload(
            item_id="answer", content=(TextPart(text=text),), complete=True
        ).model_dump(mode="json"),
        native=NativeProvenance(provider="openai", api_revision="offline"),
    )
    with pytest.raises(FleetRefused, match="result_contract_invalid"):
        ports.validate_contract({"format": format_}, event)


async def test_funded_admission_reuses_the_durable_operation_without_redispatch(sm):
    parent, run = await setup(sm, provider="openai", funds="10", cap="2")
    async with sm.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=run.tenant_id, policy=POLICY)
    delivered = []

    async def dispatch(parent, worker, resolved, reviewed):
        # Observe actual committed admission, without asserting native execution.
        current_run, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
        assert workers == (worker,)
        assert current_run.reserved_usd >= worker.budget_usd
        delivered.append(worker.id)

    ports = HostFleetPorts(sm, prices=tariffs(parent), markup=Decimal(1), dispatch=dispatch)
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": Decimal(1)}),
    )
    arguments = dict(
        run_id=run.id,
        operation_key="once",
        role="review the input",
        session=None,
        result_contract={"format": "text"},
        reviewer_of=None,
        reviewed_result=None,
    )
    first = await ports.admit(parent, resolved, **arguments)
    second = await ports.admit(parent, resolved, **arguments)
    assert first[1] == second[1]
    assert delivered == [first[1].id]
    with pytest.raises(OperationConflict):
        await ports.admit(parent, resolved, **(arguments | {"role": "changed input"}))
    assert delivered == [first[1].id]


async def test_refused_funding_commits_the_refusal_and_never_dispatches(sm):
    parent, run = await setup(sm, provider="openai", funds="0", cap="2")
    async with sm.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=run.tenant_id, policy=POLICY)

    async def forbidden(*args):
        pytest.fail("refused funding reached native dispatch")

    ports = HostFleetPorts(sm, prices=tariffs(parent), markup=Decimal(1), dispatch=forbidden)
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": Decimal(1)}),
    )
    with pytest.raises(FleetRefused, match="financial_admission_refused"):
        await ports.admit(
            parent,
            resolved,
            run_id=run.id,
            operation_key="refused",
            role="worker",
            session=None,
            result_contract=None,
            reviewer_of=None,
            reviewed_result=None,
        )
    _, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert len(workers) == 1 and workers[0].status == "cancelled"
