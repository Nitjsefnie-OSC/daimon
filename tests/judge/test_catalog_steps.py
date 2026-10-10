"""Native prefix execution, deadline cancellation and source mutants."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from daimon.testing.outcome_oracle import TurnEvidence
from daimon.testing.provider_fixtures import FixtureGap, load_provider_fixtures

from .catalog_runner import Invocation, load_catalog, plan_scenario
from .catalog_steps import StepReceipt, run_steps
from .headless_executor import execute_cell, execute_gemini_cell, execute_openai_cell
from .test_catalog_runner import planned, source
from .test_headless_executor import scripted

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "packages/testing/fixtures/target53"
CATALOG = FIXTURES / "catalog"


@pytest.mark.parametrize("backend", ("openai", "gemini"))
@pytest.mark.parametrize(
    "scenario_id",
    [
        "QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS",
        "QA-D7-BIG-WORKSPACE-CHECKPOINT",
        "QA-NEW12-AGENT-SWITCH-HANDOFF-COPY",
    ],
)
async def test_real_native_turn_prefix_stops_at_the_original_admin(
    backend: Literal["openai", "gemini"], scenario_id: str
) -> None:
    pack = load_provider_fixtures(FIXTURES / "index.json", catalog_root=CATALOG)
    scenario, fixture = pack.select(scenario_id, backend)
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, backend, run_id="prefix")
    result = await (execute_openai_cell if backend == "openai" else execute_gemini_cell)(
        plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
    )
    assert result.status == "PENDING" and result.reported_status == "PENDING"
    assert result.host_outcome.status == "PASS"
    assert result.request_count == (6 if backend == "openai" else 3)
    assert [t.turn for t in result.evidence.turns] == [1]
    assert [r.location for r in result.step_receipts] == [i.location for i in plan.invocations]
    assert [r.status for r in result.step_receipts[:3]] == ["executed"] * 3
    assert result.step_receipts[3].kind == "admin"
    assert all(r.status == "pending" and r.root_turn_id is None for r in result.step_receipts[3:])
    assert result.step_receipts[2].root_turn_id == result.evidence.turns[0].root_turn_id
    assert any(g.code == "STEP_BINDING_UNAVAILABLE" for g in result.gaps)
    assert any(g.code == "CATALOG_CAPABILITY_PENDING" for g in result.gaps)
    assert len(result.catalog_outcome.checks) == len(plan.assertions) + len(plan.global_assertions)
    assert not any(
        t.tool_capture_complete or t.approval_capture_complete for t in result.evidence.turns
    )


async def test_measured_wait_and_explicit_deadline_through_real_anthropic_host(
    tmp_path: Path,
) -> None:
    row = source()
    row["surface"] = "headless"
    row["steps"][2]["timeout_s"] = 5.0
    row["steps"].insert(3, {"do": "wait", "s": 0.01})
    plan = planned(tmp_path, row)
    result = await execute_cell(plan, scripted(plan))
    assert result.status == "PASS" and result.request_count == 6
    assert len(result.step_receipts) == len(plan.invocations)
    wait = next(r for r in result.step_receipts if r.kind == "wait")
    assert wait.status == "executed" and wait.delay_s == 0.01
    assert wait.finished_s is not None and wait.started_s is not None
    assert wait.finished_s - wait.started_s >= 0.01
    done = result.step_receipts[2]
    assert done.kind == "wait_done" and done.timeout_s == 5.0
    assert done.root_turn_id == result.evidence.turns[0].root_turn_id


async def test_real_deadline_cancels_and_drains_the_owned_turn(tmp_path: Path) -> None:
    row = source()
    row["steps"][2]["timeout_s"] = 0.01
    plan = planned(tmp_path, row)
    cancelled = asyncio.Event()

    async def invoke(_i: Invocation, _timeout: float) -> TurnEvidence:
        try:
            await asyncio.Event().wait()
            raise RuntimeError("unreachable")
        finally:
            cancelled.set()

    async def prepare(_i: Invocation) -> None:
        return None

    with pytest.raises(TimeoutError):
        await run_steps(plan, invoke=invoke, prepare_channel=prepare)
    assert cancelled.is_set()


@pytest.mark.parametrize("mutation", ["drop", "reorder", "source", "operation", "number"])
async def test_source_operation_mutants_refuse_before_any_hook(
    tmp_path: Path, mutation: str
) -> None:
    row = source()
    row["steps"].insert(3, {"do": "admin", "tool": "cli", "args": "never execute"})
    plan = planned(tmp_path, row)
    steps = list(plan.invocations)
    if mutation == "drop":
        steps.pop(3)
    elif mutation == "reorder":
        steps[3], steps[4] = steps[4], steps[3]
    elif mutation == "source":
        steps[3] = steps[3].model_copy(update={"source": {"do": "wait", "s": 0}})
    elif mutation == "operation":
        steps[3] = steps[3].model_copy(update={"operation": "new_channel"})
    else:
        steps[1] = steps[1].model_copy(update={"turn": 7})
    plan = plan.model_copy(update={"invocations": tuple(steps)})

    async def no_invoke(_i: Invocation, _timeout: float) -> TurnEvidence:
        raise AssertionError("corrupt plan reached turn hook")

    async def no_prepare(_i: Invocation) -> None:
        raise AssertionError("corrupt plan reached channel hook")

    with pytest.raises(ValueError):
        await run_steps(plan, invoke=no_invoke, prepare_channel=no_prepare)


async def test_unknown_runner_hook_is_not_a_remote_effect_exception(tmp_path: Path) -> None:
    plan = planned(tmp_path, source())

    async def no_invoke(_i: Invocation, _timeout: float) -> TurnEvidence:
        raise AssertionError("unknown hook reached provider")

    async def prepare(_i: Invocation) -> None:
        return None

    steps = await run_steps(
        plan,
        invoke=no_invoke,
        prepare_channel=prepare,
        native_gaps=(
            FixtureGap(code="RUNNER_HOOK", location="steps[1]", reason="unknown remote work"),
        ),
    )
    assert any(g.code == "PROVIDER_RUNNER_HOOK" for g in steps.gaps)
    assert steps.receipts[0].status == "executed"
    assert all(r.status == "pending" for r in steps.receipts[1:])


def test_receipt_cannot_invent_a_measured_pending_or_admin_step() -> None:
    with pytest.raises(ValueError, match="measured execution"):
        StepReceipt(
            location="steps[1]", kind="mention", status="pending", code="UNBOUND", started_s=1.0
        )
    with pytest.raises(ValueError, match="owned execution hook"):
        StepReceipt(
            location="steps[1]",
            kind="admin",
            status="executed",
            code="BOUND",
            started_s=1.0,
            finished_s=2.0,
        )


def test_optimized_source_deadline_and_receipt_refusals(tmp_path: Path) -> None:
    row = source()
    row["steps"][2]["timeout_s"] = 0.01
    plan = planned(tmp_path, row)
    inputs = tmp_path / "steps.json"
    inputs.write_text(json.dumps(plan.model_dump(mode="json")))
    script = """
import asyncio,json,sys
sys.path.insert(0,sys.argv[2])
from judge.catalog_runner import ScenarioPlan
from judge.catalog_steps import run_steps, StepReceipt
p=ScenarioPlan.model_validate(json.load(open(sys.argv[1])))
async def check():
    cancelled=asyncio.Event()
    async def invoke(i,timeout):
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    async def prepare(i): pass
    try: await run_steps(p,invoke=invoke,prepare_channel=prepare)
    except TimeoutError: pass
    else: raise RuntimeError("deadline ignored under -O")
    if not cancelled.is_set(): raise RuntimeError("owned task leaked under -O")
    for index in range(len(p.invocations)):
        mutated=p.model_copy(update={"invocations":p.invocations[:index]+p.invocations[index+1:]})
        async def refuse(*args): raise RuntimeError("dropped step reached a hook")
        try: await run_steps(mutated,invoke=refuse,prepare_channel=refuse)
        except ValueError: pass
        else: raise RuntimeError("dropped original operation accepted under -O")
    try: StepReceipt(location="steps[1]",kind="admin",status="executed",code="BOUND",started_s=1,finished_s=2)
    except ValueError: pass
    else: raise RuntimeError("admin execution invented under -O")
asyncio.run(check())
"""
    run = subprocess.run(
        [sys.executable, "-O", "-c", script, str(inputs), str(REPO / "tests")],
        capture_output=True,
        timeout=60,
    )
    if run.returncode != 0:
        raise AssertionError(run.stderr.decode())


@pytest.mark.parametrize("timeout", [5.0, 180.0])
async def test_valid_catalog_deadline_binds_and_does_not_add_a_pending_gap(
    tmp_path: Path, timeout: float
) -> None:
    row = source()
    row["surface"] = "headless"
    row["steps"][2]["timeout_s"] = timeout
    plan = planned(tmp_path, row)
    result = await execute_cell(plan, scripted(plan))
    assert result.status == "PASS" and result.request_count == 6
    assert result.step_receipts[2].timeout_s == timeout
    assert result.step_receipts[1].timeout_s == min(timeout, 10.0)
    assert not any(g.code == "WAIT_DONE_TIMEOUT_UNBOUND" for g in result.gaps)


@pytest.mark.parametrize("mutation", ["foreign_root", "foreign_session", "tentative", "missing"])
async def test_wait_done_refuses_any_foreign_or_unverified_terminal(
    tmp_path: Path, mutation: str
) -> None:
    plan = planned(tmp_path, source())
    baseline = await execute_cell(plan, scripted(plan))
    evidence = baseline.evidence.turns[0]
    terminal = evidence.terminals[0]
    if mutation == "missing":
        evidence = evidence.model_copy(update={"terminals": ()})
    else:
        changes = {
            "foreign_root": {"root_turn_id": "other"},
            "foreign_session": {"session_id": "other"},
            "tentative": {"authority": "tentative"},
        }
        evidence = evidence.model_copy(
            update={
                "terminals": (*evidence.terminals, terminal.model_copy(update=changes[mutation]))
            }
        )

    async def invoke(_i: Invocation, _timeout: float) -> TurnEvidence:
        return evidence

    async def prepare(_i: Invocation) -> None:
        return None

    steps = await run_steps(plan, invoke=invoke, prepare_channel=prepare)
    assert steps.receipts[2].status == "pending"
    assert steps.receipts[2].code == "WAIT_DONE_TERMINAL_UNVERIFIED"
    assert all(r.status == "pending" for r in steps.receipts[3:])
    assert any(g.code == "WAIT_DONE_TERMINAL_UNVERIFIED" for g in steps.gaps)
