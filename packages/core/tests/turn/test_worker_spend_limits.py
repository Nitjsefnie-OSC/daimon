"""Requested financial ceilings cannot reach unsupported native execution."""

import asyncio
from dataclasses import replace

import pytest
from daimon.core.headless_runner import run_worker
from daimon.core.neutral_resources import WorkerLimits, WorkerSpecRefused
from daimon.core.stores import fleet

from .test_agent_native_runtime import (
    application as application,
)
from .test_agent_native_runtime import (
    claimed_worker_request as claimed_worker_request,
)
from .test_agent_native_runtime import (
    clean_sdk_environment as clean_sdk_environment,
)
from .test_agent_native_runtime import (
    clone_source as clone_source,
)
from .test_agent_native_runtime import (
    db_preflight as db_preflight,
)
from .test_agent_native_runtime import (
    db_session_factory as db_session_factory,
)
from .test_agent_native_runtime import (
    explicit_worker_request as explicit_worker_request,
)
from .test_agent_native_runtime import (
    owned as owned,
)
from .test_agent_native_runtime import (
    owned_database_preflight as owned_database_preflight,
)
from .test_agent_native_runtime import (
    provider as provider,
)


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("field", ["max_model_requests", "max_input_tokens", "max_output_tokens"])
async def test_requested_unsupported_ceiling_cannot_allocate_or_consume_a_worker_input(
    claimed_worker_request, field
):
    request, root, worker, prices, owned, native_requests = claimed_worker_request
    limits = WorkerLimits.model_validate(request.worker.limits.model_dump() | {field: 1})
    candidate = replace(
        request,
        worker=replace(
            request.worker,
            limits=limits,
            permissions=replace(request.worker.permissions, limits=limits),
        ),
    )
    with pytest.raises(WorkerSpecRefused, match="budget: unsupported"):
        await run_worker(
            candidate,
            parent=root,
            worker=worker,
            trigger_message="Complete the assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert native_requests == []
    assert all(call.method == "GET" for call in owned.wire.requests)
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db,
            tenant_id=request.tenant_id,
            actor_id=root.fleet.account_id,
            run_id=worker.run_id,
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == worker.id)
    assert persisted.status == "starting"
    assert persisted.binding_id is None and persisted.turns_used == 0
    assert persisted.reserved_usd == 0 and persisted.result is None
