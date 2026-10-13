"""Normal ingress refuses incomplete source authority before revision publication."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from daimon.core.stores import mux_state
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.ids import ModelRef, ResourceRef, Revision
from mux.contracts.resources import Agent, AgentSpec

from .test_backend_selection import (
    initial_backend as initial_backend,
)
from .test_backend_selection import (
    preference_for,
)
from .test_backend_selection import (
    selector_host as selector_host,
)
from .test_provider_dispatch import REVISION, TENANT


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("accepted", [False, True])
async def test_real_admission_refuses_even_a_reader_accepting_the_native_component(
    selector_host, provider, accepted
):
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, provider, 0, actor)
    reads = []

    async def proof(request):
        candidate = request.admission.backend_revision
        assert candidate is not None and candidate.backend == provider
        assert request.admission.backend_revision_origin == "agent"
        assert request.admission.agent.id == "ag_parity_test"
        assert request.scope.tenant_id == str(TENANT)
        assert request.scope.account_id == str(request.admission.account_id)
        async with host.sessions() as db:
            assert (
                await mux_state.get_config_revision(db, candidate.channel, candidate.local) is None
            )
        row = AgentRevision(
            catalog_id="verified",
            local_revision=1,
            principal_id=request.scope.principal_id,
            agent=Agent(
                ref=ResourceRef(
                    id="native-counterpart",
                    kind="agent",
                    provider=provider,
                    account_scope_id="project",
                    tenant_id=request.scope.tenant_id,
                    account_id=request.scope.account_id,
                ),
                revision=Revision(local=0 if provider == "openai" else 1),
                spec=AgentSpec(
                    name="retained", model=ModelRef(provider=provider, id=candidate.model)
                ),
                created_at=datetime.now(UTC),
            ),
        )
        reads.append((candidate, row))
        if not accepted:
            raise RuntimeError("private provider diagnostic")
        return row

    existing = next(iter(host.deps.turn_runtimes.values()))
    profile = "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse"
    deps = replace(
        host.deps,
        agent_backends=True,
        turn_runtimes={profile: replace(existing, read_agent_counterpart=proof)},
    )
    kwargs = dict(
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        channel_id="channel",
        thread_id="new",
        now=datetime.now(UTC),
    )
    with pytest.raises(AdmissionDenied, match="backend_unsupported") as refused:
        await admit_impl(deps, **kwargs)
    assert "private" not in str(refused.value)
    async with host.sessions() as db:
        assert await mux_state.latest_config_revision(db, REVISION.channel) is None
        assert await mux_state.next_config_local(db, REVISION.channel) == 1
    assert reads == [], "refusal must precede native component readback and INSERT"
    assert host.wire.requests == [] and host.created == []
