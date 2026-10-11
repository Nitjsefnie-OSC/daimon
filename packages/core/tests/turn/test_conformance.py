"""Host selection facts and real wake storage behind explicit offline conformance envelopes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsSessionRetriesExhausted
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn import driver
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import GeminiTurnIO
from daimon.core.turn.io import TurnEvent
from daimon.core.turn.openai_codec import display_event
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.termination import TerminationReason
from daimon.testing.db import isolated_test_schema, require_owned_sessionmaker
from daimon.testing.factories import make_tenant
from daimon.testing.isolated_sdk import AmbientSDKConfigurationError, build_async_anthropic
from daimon.testing.ma_transport import ScriptedTransport
from daimon.testing.turn_conformance import (
    BackendSelectionEnvelope,
    WakeEnvelope,
    backend_selection_evidence,
    wake_evidence,
)
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import ScopeViolation
from mux.state.memory import MemoryStateStore
from mux.state.store import binding_slot
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import make_status_idle
from .test_provider_dispatch import ACCOUNT, REVISION, TENANT, admission, record_usage

SCOPE = Scope(
    tenant_id=str(TENANT), account_id=str(ACCOUNT), principal_id="host", authorization_id="offline"
)


def session(provider: str, id_: str = "session") -> ResourceRef:
    return ResourceRef(
        provider=provider,
        kind="session",
        id=id_,
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
        account_scope_id="offline-project",
    )


def terminal(provider: str, outcome: str) -> Event:
    return Event.model_validate(
        {
            "id": "ended:" + outcome,
            "session_id": "session",
            "sequence": 0,
            "type": "session.turn_ended",
            "authority": "record",
            "observed_at": datetime.now(UTC),
            "turn_id": "root",
            "payload": {"root_turn_id": "root", "outcome": outcome},
            "native": NativeProvenance(provider=provider, api_revision="offline"),
        }
    )


class OfflineIO:
    """Script TurnIO controls; use display codecs and the host lease/pump.

    These controls bypass provider stream/replay checks and do not certify a
    native send, terminal mapping, or a complete provider case envelope.
    """

    def __init__(self, event: Event, persistence: TurnPersistence | None = None):
        self.event = event
        self.persistence = persistence
        self.sent = False
        if event.native.provider == "anthropic":
            self.frame = TurnEvent(
                make_status_idle(
                    event_id=event.id,
                    stop_reason=BetaManagedAgentsSessionRetriesExhausted(type="retries_exhausted"),
                ),
                event,
            )
        elif event.native.provider == "openai":
            self.frame = TurnEvent(display_event(event, session_id=event.session_id), event)
        else:
            backend = SimpleNamespace(
                capabilities=lambda: SimpleNamespace(profile_id="gemini.inline_reuse")
            )
            self.frame = GeminiTurnIO(backend, SCOPE, session("gemini")).frame(event, {})

    async def open_stream(self, **kwargs):
        return self

    async def send(self, events):
        self.sent = True
        if self.persistence is not None:
            await self.persistence.record(session(self.event.native.provider), self.event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        assert self.sent, "host must send before consuming the terminal record"
        if self.frame is None:
            raise StopAsyncIteration
        frame, self.frame = self.frame, None
        return frame

    async def close(self):
        pass


@pytest.fixture(params=[False, True])
async def host(db_preflight, db_engine: AsyncEngine, db_clean, request):
    from daimon.core._models import Tenant

    del db_preflight  # Admission precedes every engine/cleanup dependency.
    if db_engine.url.database == "daimon_test" and not (
        os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
    ):
        pytest.skip("host hook proofs require an owned lane database or isolated CI schema")
    routes = {Tenant: db_engine, Tenant.__table__: db_engine} if request.param else None
    sm = async_sessionmaker(db_engine, expire_on_commit=False, binds=routes)
    require_owned_sessionmaker(sm)
    async with sm.begin() as db:
        await make_tenant(db, id=TENANT)
    return sm


async def run_host(monkeypatch, event, persistence=None):
    io = OfflineIO(event, persistence)
    monkeypatch.setattr(driver, "turn_io", lambda *args, **kwargs: io)
    transport = ScriptedTransport()
    async with build_async_anthropic(transport=httpx.MockTransport(transport.dispatch)) as client:
        state = await driver.run_turn(
            anthropic=client,
            session_id=event.session_id,
            user_message="offline",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="headless-unrecorded"),
            path="mux",
            scope=SCOPE,
            render_interval_s=3600,
        )
    assert not transport.requests
    return state


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("journal_mode", ["matched", "omitted", "changed-payload"])
@pytest.mark.parametrize("ci_shape", [False, True])
async def test_c17_real_wake_takeover_binding_generation_and_driver_lease(
    host, monkeypatch, provider, journal_mode, ci_shape
):
    from daimon.testing.db import isolated_test_schema
    from sqlalchemy import text

    engine = host.kw["bind"]
    physical_database = engine.url.database
    if ci_shape:
        # Change URL metadata only. The actual connection creator still targets
        # this worker's owned lane DB/schema; never open the local shared DB.
        assert isolated_test_schema(engine) is not None
        monkeypatch.setattr(engine.sync_engine, "url", engine.url.set(database="daimon_test"))
        for flag in ("GITHUB_ACTIONS", "CI"):
            monkeypatch.setenv(flag, "true")
    async with host() as db:
        assert await db.scalar(text("SELECT current_database()")) == physical_database
        assert await db.scalar(text("SELECT current_schema()")) == isolated_test_schema(engine)
    store = PostgresStateStore(host)
    profile = "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse"
    before = ProviderBinding(
        id="binding",
        thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
        provider=provider,
        profile=profile,
        native_refs={"session": "old-session"},
        generation=1,
        config_revision=1,
        legacy_account_id=str(ACCOUNT),
    )
    await store.put_binding(before, expected_generation=0)
    replacement = before.model_copy(update={"generation": 2, "native_refs": {"session": "session"}})
    request = ContinuationRequest(
        tenant_id=TENANT,
        platform="slack",
        parent_channel_id="channel",
        thread_id="thread",
        requester_account_id=ACCOUNT,
        requester_external_user_id="caller",
        target_ma_agent_id="agent",
        target_name="offline",
        requested_work="offline wake",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    event = terminal(provider, "completed")
    recorded = (
        event.model_copy(update={"payload": dict(event.payload) | {"native_reason": "different"}})
        if journal_mode == "changed-payload"
        else event
    )

    async def run(persistence):
        result = await run_host(
            monkeypatch, recorded, persistence if journal_mode != "omitted" else None
        )
        assert result.termination == TerminationReason.COMPLETED

    envelope = WakeEnvelope(
        host, store, SCOPE, request, replacement, session(provider), event, datetime.now(UTC), run
    )
    facts = await wake_evidence(envelope)
    assert facts["before_generation"] == 1
    assert facts["current_generation"] == facts["replacement_generation"] == 2
    assert facts["stale_binding_refused"] and facts["stale_pump_calls"] == 0
    assert not facts["stale_wake_started"] and not facts["stale_wake_settled"]
    assert facts["wake_started"] and facts["wake_settled"]
    assert facts["wake_status"] == "delivered" and facts["pump_calls"] == 1
    assert facts["journal_event_present"] is (journal_mode == "matched")
    assert facts["lease_released"]
    with pytest.raises(ValueError, match="next generation"):
        await wake_evidence(envelope)
    with pytest.raises(ScopeViolation):
        await wake_evidence(
            replace(envelope, scope=SCOPE.model_copy(update={"account_id": "foreign"}))
        )


@pytest.mark.parametrize("retain_original", [True, False])
async def test_c14_envelope_captures_prepared_identity_and_refusal_without_verdict(retain_original):
    first = PreparedTurn(
        admission(),
        "old-session",
        None,
        None,
        False,
        ACCOUNT,
        record_usage,
        session_ref=session("openai", "old-session"),
    )
    store = MemoryStateStore()
    await store.put_config_revision(REVISION)
    next_revision = ConfigRevision.create(
        REVISION.channel,
        2,
        resolve_default(
            BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")
        ),
    )
    original_binding = ProviderBinding(
        id="original",
        thread=ThreadRef(channel=REVISION.channel, thread_id="existing"),
        provider="openai",
        profile=REVISION.profile,
        native_refs={"session": "old-session"},
        generation=1,
        config_revision=1,
        legacy_account_id=str(ACCOUNT),
    )
    changed = replace(
        first,
        admission=replace(
            first.admission,
            backend_revision=next_revision,
        ),
        ma_session_id="new-session",
        session_ref=session("gemini", "new-session"),
    )
    order = []

    async def prepare_first():
        order.append("first")
        await store.put_binding(original_binding, expected_generation=0)
        return first

    async def change():
        order.append("change")
        await store.put_config_revision(next_revision)

    async def existing():
        order.append("existing")
        return replace(first if retain_original else changed, reused=True)

    async def new():
        order.append("new")
        await store.put_binding(
            original_binding.model_copy(
                update={
                    "id": "new",
                    "thread": ThreadRef(channel=REVISION.channel, thread_id="new"),
                    "provider": "gemini",
                    "profile": next_revision.profile,
                    "config_revision": 2,
                    "native_refs": {"session": "new-session"},
                }
            ),
            expected_generation=0,
        )
        return changed

    async def unavailable():
        order.append("missing-original")
        raise AdmissionDenied(reason="backend_unsupported")

    facts = await backend_selection_evidence(
        BackendSelectionEnvelope(
            store,
            SCOPE,
            REVISION.channel,
            "existing",
            "new",
            prepare_first,
            change,
            existing,
            new,
            unavailable,
            lambda: 0,
        )
    )
    assert order == ["first", "change", "existing", "new", "missing-original"]
    assert facts["first"] == ("openai.persistent_workspace", 1, "old-session", "openai")
    assert (facts["existing"] == facts["first"]) is retain_original
    assert facts["new_thread"] == ("gemini.inline_reuse", 2, "new-session", "gemini")
    assert facts["original_config"] == ("openai.persistent_workspace", 1)
    assert facts["changed_config"] == ("gemini.inline_reuse", 2)
    assert facts["first_binding"] == facts["existing_binding"] == facts["first"]
    assert (facts["existing"] == facts["existing_binding"]) is retain_original
    assert facts["new_binding"] == facts["new_thread"]
    assert facts["missing_original_refusal"] == "backend_unsupported"
    assert facts["missing_original_mutations"] == 0
    assert "status" not in facts


@pytest.mark.parametrize("connection", [False, True])
@pytest.mark.parametrize(
    "database,host,query,registered,schema_kind,flags",
    [
        ("daimon", "localhost", "", True, "worker", True),
        ("daimon_test", "localhost", "", True, "worker", False),
        ("daimon_test", "localhost", "", False, "worker", True),
        ("daimon_test", "localhost", "", True, "public", True),
        ("daimon_test", "localhost", "", True, "foreign", True),
        ("daimon_test", "localhost", "", True, "bad_nonce", True),
        ("daimon_test_nc_", "localhost", "", True, "worker", True),
        ("daimon_test_nc_N4", "remote.invalid", "", True, "worker", True),
        ("daimon_test_nc_N4", "localhost", "?host=remote.invalid", True, "worker", True),
        ("daimon_test", "remote.invalid", "", True, "worker", True),
    ],
)
async def test_database_guard_refuses_before_any_connection(
    monkeypatch, connection, database, host, query, registered, schema_kind, flags
):
    import os

    from daimon.testing.db import build_test_engine, require_owned_sessionmaker
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

    for flag in ("GITHUB_ACTIONS", "CI"):
        monkeypatch.setenv(flag, "true" if flags else "false")
    schema = {
        "worker": f"test_w{os.getpid()}_1234abcd",
        "public": "public",
        "foreign": f"test_w{os.getpid() + 1000000}_1234abcd",
        "bad_nonce": f"test_w{os.getpid()}_abc",
    }[schema_kind]
    url = f"postgresql+asyncpg://offline:fixture@{host}/{database}{query}"
    engine = (
        build_test_engine(url, schema)
        if registered
        else create_async_engine(url, execution_options={"isolated_test_schema": schema})
    )
    connections = []

    def forbid_connection(*args, **kwargs):
        connections.append(None)
        raise AssertionError("guard attempted a database connection")

    event.listen(engine.sync_engine, "do_connect", forbid_connection)
    bind = AsyncConnection(engine) if connection else engine
    try:
        with pytest.raises(ValueError, match="owned local lane"):
            require_owned_sessionmaker(async_sessionmaker(bind))
        assert connections == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["w", "f"])
async def test_shared_ci_requires_actual_registered_schema(monkeypatch, kind):
    import os

    from daimon.testing import db as testing_db
    from daimon.testing.db import require_owned_sessionmaker
    from sqlalchemy import event

    for flag in ("GITHUB_ACTIONS", "CI"):
        monkeypatch.setenv(flag, "true")
    engine = testing_db.build_test_engine(
        "postgresql+asyncpg://offline:fixture@localhost/daimon_test",
        f"test_{kind}{os.getpid()}_1234abcd",
    )
    connections = []

    def forbid_connection(*args, **kwargs):
        connections.append(None)
        raise AssertionError("guard attempted a database connection")

    event.listen(engine.sync_engine, "do_connect", forbid_connection)
    try:
        assert testing_db.isolated_test_schema(engine) == f"test_{kind}{os.getpid()}_1234abcd"
        require_owned_sessionmaker(async_sessionmaker(engine))
        assert connections == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("foreign", ["platform", "account", "tenant", "same-thread"])
async def test_c14_foreign_envelope_refuses_before_callbacks(foreign):
    calls = []

    async def forbidden():
        calls.append(None)
        raise AssertionError("foreign envelope reached a callback")

    scope = SCOPE
    if foreign == "platform":
        scope = Scope.platform(reason="offline control")
    elif foreign == "tenant":
        scope = SCOPE.model_copy(update={"tenant_id": "foreign"})
    # An empty or foreign account is checked against the prepared identity;
    # the channel itself has no account identity. Exercise that boundary below.
    if foreign == "account":
        prepared = PreparedTurn(
            admission(),
            "old-session",
            None,
            None,
            False,
            ACCOUNT,
            record_usage,
            session_ref=session("openai", "old-session"),
        )
        scope = SCOPE.model_copy(update={"account_id": "foreign"})

        async def first():
            calls.append("first")
            return prepared
    else:
        first = forbidden
    envelope = BackendSelectionEnvelope(
        MemoryStateStore(),
        scope,
        REVISION.channel,
        "existing",
        "existing" if foreign == "same-thread" else "new",
        first,
        forbidden,
        forbidden,
        forbidden,
        forbidden,
        lambda: 0,
    )
    with pytest.raises(ScopeViolation):
        await backend_selection_evidence(envelope)
    assert calls == (["first"] if foreign == "account" else [])


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("connection", [False, True])
@pytest.mark.parametrize("unsafe_first", [False, True])
@pytest.mark.parametrize("unsafe_table", [False, True])
@pytest.mark.parametrize(
    "target",
    [
        "localhost/daimon",
        "localhost/daimon_test",
        "remote.invalid/daimon_test_nc_N4",
        "localhost/daimon_test_nc_N4?host=remote.invalid",
    ],
)
async def test_c17_entrypoint_guards_every_route_before_store_or_callback(
    routed, connection, unsafe_first, unsafe_table, target, monkeypatch
):
    from daimon.core._models import TaskContinuation
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

    for flag in ("GITHUB_ACTIONS", "CI"):
        monkeypatch.setenv(flag, "true")
    engine = create_async_engine(f"postgresql+asyncpg://offline:fixture@{target}")
    owned = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_N4")
    connections, callbacks, store_calls = [], [], []

    def forbid_connection(*args, **kwargs):
        connections.append(None)
        raise AssertionError("wake guard reached a database connection")

    async def forbidden(persistence):
        callbacks.append(None)
        raise AssertionError("wake guard reached the driver")

    for observed in (engine, owned):
        event.listen(observed.sync_engine, "do_connect", forbid_connection)
    unsafe_bind = AsyncConnection(engine) if connection else engine
    if routed:
        routes = (
            [(TaskContinuation, owned), (TaskContinuation.__table__, unsafe_bind)]
            if unsafe_table
            else [(TaskContinuation.__table__, owned), (TaskContinuation, unsafe_bind)]
        )
        if unsafe_first:
            routes.reverse()
        sessions = async_sessionmaker(owned, binds=dict(routes))
    else:
        sessions = async_sessionmaker(unsafe_bind)
    store = MemoryStateStore()
    binding = ProviderBinding(
        id="guard-control",
        thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
        provider="openai",
        profile=REVISION.profile,
        native_refs={"session": "session"},
        generation=1,
        config_revision=1,
        legacy_account_id=str(ACCOUNT),
    )
    await store.put_binding(binding, expected_generation=0)

    async def forbidden_store(slot):
        store_calls.append(None)
        raise AssertionError("wake guard reached its store")

    monkeypatch.setattr(store, "get_binding", forbidden_store)
    request = ContinuationRequest(
        tenant_id=TENANT,
        platform="slack",
        parent_channel_id="channel",
        thread_id="thread",
        requester_account_id=ACCOUNT,
        requester_external_user_id="caller",
        target_ma_agent_id="agent",
        target_name="offline",
        requested_work="offline wake",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    try:
        with pytest.raises(ValueError, match="owned local lane"):
            await wake_evidence(
                WakeEnvelope(
                    sessions,
                    store,
                    SCOPE,
                    request,
                    binding.model_copy(update={"generation": 2}),
                    session("openai"),
                    terminal("openai", "completed"),
                    datetime.now(UTC),
                    forbidden,
                )
            )
        assert connections == callbacks == store_calls == []
        assert await MemoryStateStore.get_binding(store, binding_slot(binding)) == binding
    finally:
        await engine.dispose()
        await owned.dispose()


@pytest.mark.parametrize("prefix", ["ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"])
@pytest.mark.parametrize("value", ["", "offline-value"])
async def test_host_control_refuses_provider_environment_before_dispatch(
    monkeypatch, prefix, value
):
    monkeypatch.setenv(prefix + "FUTURE_N4_SETTING", value)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(None)
        raise AssertionError("ambient configuration reached host dispatch")

    monkeypatch.setattr(driver, "run_turn", forbidden)
    with pytest.raises(
        AmbientSDKConfigurationError, match="^test SDK refuses ambient provider configuration$"
    ):
        await run_host(monkeypatch, terminal("openai", "completed"))
    assert calls == []


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize(
    "github_actions,ci",
    [
        (None, None),
        ("true", None),
        (None, "true"),
        ("True", "true"),
        ("true", "True"),
        ("", "true"),
        ("true", ""),
        ("true", "false"),
    ],
)
async def test_host_fixture_skips_contributor_database_before_provisioning(
    monkeypatch, routed, github_actions, ci
):
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    for flag, value in (("GITHUB_ACTIONS", github_actions), ("CI", ci)):
        if value is None:
            monkeypatch.delenv(flag, raising=False)
        else:
            monkeypatch.setenv(flag, value)
    engine = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(None)
        raise AssertionError("contributor fixture reached provisioning or a connection")

    event.listen(engine.sync_engine, "do_connect", forbidden)
    monkeypatch.setattr(sys.modules[__name__], "make_tenant", forbidden)
    try:
        with pytest.raises(pytest.skip.Exception, match="owned lane database or isolated CI"):
            await host.__wrapped__(None, engine, None, SimpleNamespace(param=routed))
        assert calls == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("routed", [False, True])
async def test_host_fixture_refuses_unregistered_ci_before_provisioning(monkeypatch, routed):
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    engine = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(None)
        raise AssertionError("unregistered CI fixture reached provisioning or a connection")

    event.listen(engine.sync_engine, "do_connect", forbidden)
    monkeypatch.setattr(sys.modules[__name__], "make_tenant", forbidden)
    try:
        with pytest.raises(ValueError, match="registered isolated CI"):
            await host.__wrapped__(None, engine, None, SimpleNamespace(param=routed))
        assert calls == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("routed", [False, True])
async def test_host_fixture_preserves_registered_ci_provisioning(
    db_preflight, db_engine, db_clean, monkeypatch, routed
):
    from sqlalchemy import text

    if db_engine.url.database == "daimon_test" and not (
        os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
    ):
        pytest.skip("host hook proofs require an owned lane database or isolated CI schema")
    require_owned_sessionmaker(db_engine)
    async with db_engine.connect() as db:
        physical_database = await db.scalar(text("SELECT current_database()"))
    schema = isolated_test_schema(db_engine)
    assert schema is not None
    # URL metadata only; the existing pool still connects to this owned lane DB.
    monkeypatch.setattr(db_engine.sync_engine, "url", db_engine.url.set(database="daimon_test"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    sessions = await host.__wrapped__(db_preflight, db_engine, None, SimpleNamespace(param=routed))
    async with sessions() as db:
        assert await db.scalar(text("SELECT current_database()")) == physical_database
        assert await db.scalar(text("SELECT current_schema()")) == schema


def run_host_fixture_preflight_probe(tmp_path: Path, *, target: str, case: str) -> None:
    """Run the actual host fixture graph with allocation/connection traps."""
    plugin = tmp_path / "n4_preflight_probe.py"
    receipt = tmp_path / "receipt.json"
    plugin.write_text(
        """
import json
import os
from pathlib import Path
import pytest
import daimon.testing.db as shared
from sqlalchemy import event
from sqlalchemy.engine import Engine

counts = {"builds": 0, "connections": 0, "host_setup": 0}
patch = pytest.MonkeyPatch()

def pytest_sessionstart(session):
    original = shared.build_test_engine
    def allocate(*args, **kwargs):
        counts["builds"] += 1
        return original(*args, **kwargs)
    def forbidden_connection(*args, **kwargs):
        counts["connections"] += 1
        raise RuntimeError("N4_ACCEPTED_CONFIGURATION_CONNECTION_TRAP")
    patch.setattr(shared, "build_test_engine", allocate)
    event.listen(Engine, "do_connect", forbidden_connection)
    if os.environ["N4_PREFLIGHT_CASE"] == "ci_foreign_schema":
        patch.setattr(shared, "_new_schema_name",
                      lambda kind: f"test_w{os.getpid() + 1}_1234abcd")

def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname == "host":
        counts["host_setup"] += 1

def pytest_sessionfinish(session, exitstatus):
    counts["exitstatus"] = int(exitstatus)
    Path(os.environ["N4_PREFLIGHT_RECEIPT"]).write_text(json.dumps(counts))
"""
    )
    urls = {
        "foreign": "postgresql+asyncpg://fixture:fixture@localhost/operator_test",
        "remote": "postgresql+asyncpg://fixture:fixture@remote.invalid/daimon_test_nc_N4",
        "query": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test_nc_N4?host=remote.invalid",
        "contributor": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test",
        "lane": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test_nc_N4_probe",
        "ci": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test",
        "ci_foreign_schema": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test",
    }
    # Do not read/copy operator credential values into the child process.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ") if key in os.environ}
    env.update(
        DAIMON_DATABASE__TEST_URL=urls[case],
        PYTHONPATH=str(tmp_path),
        N4_PREFLIGHT_CASE=case,
        N4_PREFLIGHT_RECEIPT=str(receipt),
    )
    if case.startswith("ci"):
        env.update(CI="true", GITHUB_ACTIONS="true")
    child = subprocess.run(
        [
            sys.executable,
            *(["-O"] if sys.flags.optimize else []),
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-n",
            "0",
            "-p",
            "n4_preflight_probe",
            target,
        ],
        cwd=Path(__file__).resolve().parents[4],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    observed = json.loads(receipt.read_text())
    accepted = case in {"lane", "ci"}
    expected = {"builds": int(accepted), "connections": int(accepted), "host_setup": 0}
    if {name: observed[name] for name in expected} != expected:
        raise RuntimeError(f"host fixture reached work before admission: {observed}")
    if case == "contributor":
        if child.returncode != 0 or "skipped" not in child.stdout:
            raise RuntimeError("plain contributor fixture did not skip before allocation")
    elif child.returncode != 1:
        raise RuntimeError(f"fixture refusal/trap was not executed: {child.stdout}{child.stderr}")
    elif accepted and "N4_ACCEPTED_CONFIGURATION_CONNECTION_TRAP" not in child.stdout:
        raise RuntimeError("accepted lane/registered CI did not reach the deliberate trap")
    elif not accepted and (
        "Test hooks require owned local lane engines or registered isolated CI schemas"
        not in child.stdout
    ):
        raise RuntimeError(
            f"shared preflight refusal was not executed: {child.stdout}{child.stderr}"
        )


@pytest.mark.parametrize(
    "case", ["foreign", "remote", "query", "contributor", "lane", "ci", "ci_foreign_schema"]
)
def test_host_fixture_real_preflight_precedes_database_setup(tmp_path: Path, case: str) -> None:
    run_host_fixture_preflight_probe(
        tmp_path,
        target=(
            "packages/core/tests/turn/test_conformance.py::"
            "test_c17_real_wake_takeover_binding_generation_and_driver_lease"
            "[False-False-matched-openai]"
        ),
        case=case,
    )
