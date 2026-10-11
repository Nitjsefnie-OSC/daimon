"""Actual normal admission keeps authorized native bindings after channel edits."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from daimon.core import channel_backend
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
from daimon.core.stores import mux_state
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_host import OpenAIHostRuntime, prepare_openai
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.prepare import ProviderPreparationRequest, bind_session_impl
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_tenant_config,
)
from daimon.testing.isolated_sdk import build_async_anthropic, build_async_openai
from daimon.testing.ma import json_body, make_fake_memory_store_handler
from daimon.testing.ma_models import ma_session
from daimon.testing.turn_router import build_turn_router
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ResourceRef, Revision, ThreadRef
from mux.contracts.resources import SessionSpec
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.errors import ScopeViolation
from mux.state.lease import Slot
from mux.state.memory import MemoryStateStore

from .test_openai_host import SESSION_ID, Wire
from .test_provider_dispatch import ACCOUNT, PROFILE, REVISION, SCOPE, TENANT, admission, deps_for


@pytest.fixture(params=["memory", "postgres"])
async def selector_host(db_session_factory, db_clean, request):
    try:
        require_owned_sessionmaker(db_session_factory)
    except ValueError:
        pytest.skip("native selector proofs require an owned lane DB or isolated CI")
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, id=TENANT)
        account = await make_account(db, tenant=tenant, id=ACCOUNT)
        await make_platform_principal(
            db, platform="slack", external_id="caller", tenant=tenant, account=account
        )
        await make_tenant_config(
            db, tenant=tenant, agent_name="test-agent", environment_name="test-env"
        )
        await make_ledger_entry(db, tenant=tenant, delta_usd=Decimal(100))
        await channel_backend.set_channel_backend(
            db,
            REVISION.channel,
            BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"),
        )
    store = (
        MemoryStateStore() if request.param == "memory" else PostgresStateStore(db_session_factory)
    )
    await store.put_config_revision(REVISION)
    wire = Wire()
    wire.expected_spend_limit = None
    ancillary = build_turn_router(str(TENANT))
    memory = make_fake_memory_store_handler()
    calls, created = [], []

    def create(req, match):
        body = json_body(req)
        id_ = f"ancillary-session-{len(created) + 1}"
        created.append(id_)
        return httpx.Response(
            200,
            json=ma_session(
                id=id_,
                agent_id="ag_parity_test",
                environment_id="env_parity_test",
                metadata=body.get("metadata"),
                resources=body.get("resources", ()),
            ).model_dump(mode="json"),
        )

    ancillary.add("POST", r"/v1/sessions", create)

    def handle(req):
        calls.append(req)
        return (
            memory(req) if req.url.path.startswith("/v1/memory_stores") else ancillary.dispatch(req)
        )

    async def plan(request):
        return SessionSpec(
            agent=ResourceRef(
                id="native-agent",
                kind="agent",
                provider="openai",
                account_scope_id="project",
                tenant_id=SCOPE.tenant_id,
                account_id=SCOPE.account_id,
            ),
            agent_revision=Revision(local=0),
            config_revision=REVISION.local,
        )

    async with (
        build_async_openai(
            transport=httpx.MockTransport(wire.handle),
            api_key="offline",
            base_url="https://openai.invalid/v1",
        ) as sdk,
        build_async_anthropic(
            transport=httpx.MockTransport(handle),
            api_key="offline",
            base_url="https://anthropic.invalid",
        ) as client,
    ):
        runtime = OpenAIHostRuntime(
            lambda revision, scope: SDKTransport(sdk),
            OpenAIRecoveryJournal(),
            OpenAIUsageRevisions(),
            account_scope_id="project",
            session_plan=plan,
            authorization=lambda scope, kind, id_: scope == SCOPE,
            controls=SessionControls(
                model="gpt-6-luna", multi_agent_enabled=False, container_size="small"
            ),
        )
        deps = replace(
            deps_for(client, db_session_factory),
            channel_backends=True,
            state_store=store,
            turn_runtimes={PROFILE: runtime},
        )
        native = await prepare_openai(
            ProviderPreparationRequest(
                deps,
                admission(),
                SCOPE,
                TENANT,
                "slack",
                "caller",
                "thread",
                ACCOUNT,
                True,
                DEFAULT_MA_CAPABILITIES,
                None,
                datetime.now(UTC) + timedelta(seconds=30),
                lambda: datetime.now(UTC),
            )
        )
        admitted = []

        async def prepare(thread):
            current = await admit_impl(
                deps,
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                channel_id="channel",
                thread_id=thread,
                now=datetime.now(UTC),
            )
            admitted.append(current)
            return await bind_session_impl(
                deps,
                current,
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                thread_id=thread,
                session_account_id=ACCOUNT,
                reuse_existing=True,
            )

        async def change():
            async with db_session_factory.begin() as db:
                return await channel_backend.set_channel_backend(
                    db, REVISION.channel, BackendConfig()
                )

        slot = Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"), account_id=str(ACCOUNT)
        )
        yield SimpleNamespace(
            deps=deps,
            store=store,
            wire=wire,
            calls=calls,
            created=created,
            native=native,
            admitted=admitted,
            prepare=prepare,
            change=change,
            slot=slot,
        )


async def test_authorized_existing_binding_retains_revision_and_new_thread_uses_current(
    selector_host,
):
    host = selector_host
    first = await host.prepare("thread")
    binding = await host.store.get_binding(host.slot)
    assert binding is not None
    current = await host.change()
    existing = await host.prepare("thread")
    new = await host.prepare("new-thread")
    assert first.session_ref == existing.session_ref == host.native.session_ref
    assert existing.ma_session_id == SESSION_ID and existing.reused
    assert existing.admission.backend_revision == REVISION
    assert await host.store.get_binding(host.slot) == binding
    assert new.admission.backend_revision == current and current.local == 2
    assert new.admission.backend_revision.backend == "anthropic"
    assert new.session_ref is None  # Preserve the existing Anthropic prepared shape.
    assert new.ma_session_id != existing.ma_session_id
    assert [item.backend_revision for item in host.admitted] == [REVISION, REVISION, current]
    assert len([req for req in host.wire.requests if req.method == "POST"]) == 1
    assert host.created == [new.ma_session_id]
    assert all(req.url.host == "openai.invalid" for req in host.wire.requests)
    assert all(req.url.host == "anthropic.invalid" for req in host.calls)


async def test_missing_retained_provider_registration_refuses_before_any_provider_effect(
    selector_host, monkeypatch
):
    host = selector_host
    await host.change()
    before = (len(host.wire.requests), len(host.calls))
    monkeypatch.setattr(
        channel_backend, "RUNNABLE_PROFILES", channel_backend.RUNNABLE_PROFILES - {PROFILE}
    )
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.prepare("thread")
    assert (len(host.wire.requests), len(host.calls)) == before
    assert host.created == []


@pytest.mark.parametrize("defect", ["missing", "provider", "profile", "revision", "channel"])
async def test_missing_or_mismatched_retained_revision_refuses_before_provider_effect(
    selector_host, monkeypatch, defect
):
    host = selector_host
    await host.change()
    before = (len(host.wire.requests), len(host.calls))
    retained = REVISION
    if defect == "provider":
        retained = REVISION.model_copy(update={"backend": "anthropic"})
    elif defect == "profile":
        retained = REVISION.model_copy(update={"profile": "gemini.inline_reuse"})
    elif defect == "revision":
        retained = REVISION.model_copy(update={"local": 2})
    elif defect == "channel":
        retained = REVISION.model_copy(
            update={"channel": REVISION.channel.model_copy(update={"channel_id": "foreign"})}
        )

    async def read(db, channel, local):
        assert channel == REVISION.channel and local == 1
        return None if defect == "missing" else retained

    monkeypatch.setattr(mux_state, "get_config_revision", read)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await host.prepare("thread")
    assert (len(host.wire.requests), len(host.calls)) == before
    assert host.created == []


@pytest.mark.parametrize("defect", ["account", "thread"])
async def test_foreign_binding_is_refused_before_retained_config_or_provider_io(
    selector_host, monkeypatch, defect
):
    host = selector_host
    binding = await host.store.get_binding(host.slot)
    assert binding is not None
    if defect == "account":
        binding = binding.model_copy(update={"legacy_account_id": "foreign"})
    else:
        binding = binding.model_copy(
            update={"thread": binding.thread.model_copy(update={"thread_id": "foreign"})}
        )
    await host.change()
    before = (len(host.wire.requests), len(host.calls))

    async def read(slot):
        assert slot == host.slot
        return binding

    async def forbidden(*args):
        raise AssertionError("foreign binding reached config read")

    monkeypatch.setattr(host.store, "get_binding", read)
    monkeypatch.setattr(mux_state, "get_config_revision", forbidden)
    with pytest.raises(ScopeViolation, match="authorized thread"):
        await host.prepare("thread")
    assert (len(host.wire.requests), len(host.calls)) == before


@pytest.mark.parametrize("path,enabled", [("legacy", True), ("legacy", False), ("mux", False)])
async def test_legacy_or_unconfigured_admission_does_not_read_native_bindings(
    selector_host, monkeypatch, path, enabled
):
    host = selector_host
    latest = await host.change()

    async def forbidden(*args):
        raise AssertionError("legacy/unconfigured admission read a native binding")

    monkeypatch.setattr(host.store, "get_binding", forbidden)
    current = await admit_impl(
        replace(host.deps, turn_path=path, channel_backends=enabled),
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        channel_id="channel",
        thread_id="thread",
        now=datetime.now(UTC),
    )
    assert current.backend_revision == (latest if enabled else None)
    assert host.created == []


async def test_another_caller_never_selects_the_private_native_binding(selector_host):
    host = selector_host
    other = uuid.UUID(int=4410)
    from daimon.core.stores.tenants import get_tenant

    async with host.deps.sessionmaker.begin() as db:
        tenant = await get_tenant(db, TENANT)
        assert tenant is not None
        account = await make_account(db, tenant=tenant, id=other)
        await make_platform_principal(
            db, platform="slack", external_id="other", tenant=tenant, account=account
        )
    latest = await host.change()
    before = len(host.wire.requests)
    current = await admit_impl(
        host.deps,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="other",
        channel_id="channel",
        thread_id="thread",
        now=datetime.now(UTC),
    )
    assert current.account_id == other and current.backend_revision == latest
    prepared = await bind_session_impl(
        host.deps,
        current,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="other",
        thread_id="thread",
        session_account_id=other,
        reuse_existing=True,
    )
    assert prepared.ma_session_id != host.native.ma_session_id
    assert len(host.wire.requests) == before
    assert host.created == [prepared.ma_session_id]
    binding = await host.store.get_binding(host.slot)
    assert binding is not None and binding.native_refs["session"] == host.native.ma_session_id
