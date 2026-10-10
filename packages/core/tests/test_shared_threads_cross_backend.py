"""M1 across backends: an opt-in shared thread on an explicit OpenAI or Gemini channel.

Offline. The provider side is a registered stand-in preparer per profile, the
same seam the OpenAI (G1) and Gemini (G2) host codecs register on
(`daimon.core.turn.prepare.register_turn_preparation` plus `RUNNABLE_PROFILES`).
What is proven is Daimon's side: admission carries the explicit revision, every
caller's turn reaches the provider as the thread's shared owner and lands on
one native session, the binding records the provider and revision, a later
switch of the channel to the other provider leaves the thread where it was
(C14), the identity survives a restart, the journal takes only the shared
slot's lease, and an unconfigured channel never reaches any of it.

The tests marked `PENDING[G1]` / `PENDING[G2]` run against the real codecs once
those branches merge and register their preparers; until then they skip with
that typed reason.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from daimon.core import channel_backend
from daimon.core._models import AgentGitHubMode
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import McpSettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.shared_threads import shared_owner, shared_slot
from daimon.core.stores import mux_state
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn import prepare as preparation
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest, bind_session
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.config import BackendConfig, ConfigRevision
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.errors import ScopeViolation
from mux.state.journal import JournalEntry
from mux.state.lease import Slot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

OPENAI = "openai.persistent_workspace"
GEMINI = "gemini.inline_reuse"
CONFIGS = {
    OPENAI: BackendConfig(
        backend="openai", profile=OPENAI, model="gpt-6-luna", thread_mode="shared"
    ),
    GEMINI: BackendConfig(
        backend="gemini", profile=GEMINI, model="gemini-3-pro", thread_mode="shared"
    ),
}


@dataclass
class _Provider:
    """A stand-in host preparer: one native session per (thread, session owner)."""

    profile: str
    calls: list[ProviderPreparationRequest] = field(
        default_factory=list[ProviderPreparationRequest]
    )
    sessions: dict[tuple[str, uuid.UUID], str] = field(
        default_factory=dict[tuple[str, uuid.UUID], str]
    )

    async def __call__(self, request: ProviderPreparationRequest) -> PreparedTurn:
        self.calls.append(request)
        key = (request.thread_id, request.session_account_id)
        reused = key in self.sessions
        native = self.sessions.setdefault(key, f"{self.profile}:{len(self.sessions)}")
        provider = self.profile.split(".")[0]
        ref = ResourceRef(
            id=native,
            kind="session",
            provider=provider,  # pyright: ignore[reportArgumentType]
            account_scope_id=f"{provider}-project",
            tenant_id=request.scope.tenant_id,
            account_id=request.scope.account_id,
        )

        async def record(event: object) -> None:
            raise AssertionError("preparation never bills")

        return PreparedTurn(
            admission=request.admission,
            ma_session_id=native,
            mapping_id=None,
            watermark=None,
            reused=reused,
            session_account_id=request.session_account_id,
            _record=record,
            session_ref=ref,
        )


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Provider]:
    """Both profiles registered and runnable, as G1/G2's registration lines make them."""
    registered = {profile: _Provider(profile) for profile in CONFIGS}
    for profile, provider in registered.items():
        monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, provider)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(
        channel_backend,
        "RUNNABLE_PROFILES",
        channel_backend.RUNNABLE_PROFILES | frozenset(CONFIGS),
    )
    return registered


def _deps(sessionmaker: async_sessionmaker[AsyncSession]) -> TurnDeps:
    def no_anthropic(request: Any) -> Any:
        raise AssertionError(f"an explicit provider turn reached Anthropic: {request.url.path}")

    return TurnDeps(
        anthropic=build_fake_anthropic(no_anthropic),
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/unused"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal(1),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        turn_path="mux",
        channel_backends=True,
    )


@dataclass
class _World:
    tenant: Any
    alice: uuid.UUID
    bob: uuid.UUID
    agent: Any
    env: Any


async def _world(session: AsyncSession) -> _World:
    tenant = await make_tenant(session)
    alice = await make_account(session, tenant=tenant)
    bob = await make_account(session, tenant=tenant)
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_host")
    session.add(AgentGitHubMode(tenant_id=tenant.id, agent_id=agent_uuid, mode="app"))
    await session.commit()
    return _World(
        tenant, alice.id, bob.id, ma_agent(id="ag_host", tenant_id=tenant.id), ma_environment()
    )


def _admission(world: _World, account: uuid.UUID, revision: ConfigRevision | None) -> Admission:
    return Admission(
        account_id=account,
        agent=world.agent,
        environment=world.env,
        config=ResolvedConfig(agent_name="host", environment_name="host"),
        origin_channel_id="chan-x",
        backend_revision=revision,
        backend_ever_shared=revision is not None and revision.thread_mode == "shared",
    )


async def _bind(
    deps: TurnDeps,
    world: _World,
    account: uuid.UUID,
    revision: ConfigRevision | None,
    *,
    user: str,
    thread_id: str = "thread-1",
    ever_shared: bool = False,
) -> PreparedTurn:
    admission = _admission(world, account, revision)
    if ever_shared:
        admission = replace(admission, backend_ever_shared=True)
    return await bind_session(
        deps,
        admission,
        tenant_id=world.tenant.id,
        platform="slack",
        external_user_id=user,
        thread_id=thread_id,
        session_account_id=account,
        reuse_existing=True,
    )


def _slot(world: _World, thread_id: str = "thread-1") -> Slot:
    return shared_slot(world.tenant.id, "slack", "chan-x", thread_id)


@pytest.mark.parametrize("profile", [OPENAI, GEMINI])
async def test_a_shared_thread_on_an_explicit_backend_keeps_one_identity(
    profile: str,
    providers: dict[str, _Provider],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session)
    revision = await set_channel_backend(
        db_session, channel_ref(world.tenant.id, "slack", "chan-x"), CONFIGS[profile]
    )
    await db_session.commit()
    deps = _deps(db_session_factory)

    first = await _bind(deps, world, world.alice, revision, user="alice")
    second = await _bind(deps, world, world.bob, revision, user="bob")

    owner = shared_owner(_slot(world))
    provider = providers[profile]
    # Admission: the explicit revision reaches the provider, unchanged.
    assert [c.admission.backend_revision for c in provider.calls] == [revision, revision]
    # Binding: both callers reach the provider as the thread's owner, one native session.
    assert [c.session_account_id for c in provider.calls] == [owner, owner]
    assert first.ma_session_id == second.ma_session_id and second.reused
    assert (first.admission.account_id, second.admission.account_id) == (world.alice, world.bob)
    async with db_session_factory() as s:
        binding = await mux_state.get_binding(s, _slot(world))
    assert binding is not None
    assert (binding.provider, binding.profile) == (CONFIGS[profile].backend, profile)
    assert binding.native_refs["session"] == first.ma_session_id
    assert (binding.generation, binding.config_revision) == (1, revision.local)
    assert binding.id == str(owner) and binding.legacy_account_id is None
    assert providers[OPENAI if profile == GEMINI else GEMINI].calls == []


async def test_c14_a_provider_switch_moves_new_threads_only(
    providers: dict[str, _Provider],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session)
    channel = channel_ref(world.tenant.id, "slack", "chan-x")
    on_openai = await set_channel_backend(db_session, channel, CONFIGS[OPENAI])
    await db_session.commit()
    deps = _deps(db_session_factory)
    t1 = await _bind(deps, world, world.alice, on_openai, user="alice")

    on_gemini = await set_channel_backend(db_session, channel, CONFIGS[GEMINI])
    await db_session.commit()
    t1_again = await _bind(deps, world, world.bob, on_gemini, user="bob")
    t2 = await _bind(deps, world, world.alice, on_gemini, user="alice", thread_id="thread-2")

    # T1 stays on A (OpenAI, its pinned revision); T2 runs on B (Gemini).
    assert t1_again.ma_session_id == t1.ma_session_id
    assert t1_again.admission.backend_revision == on_openai
    assert [c.thread_id for c in providers[OPENAI].calls] == ["thread-1", "thread-1"]
    assert [c.thread_id for c in providers[GEMINI].calls] == ["thread-2"]
    async with db_session_factory() as s:
        b1 = await mux_state.get_binding(s, _slot(world))
        b2 = await mux_state.get_binding(s, _slot(world, "thread-2"))
    assert b1 is not None and (b1.provider, b1.config_revision) == ("openai", on_openai.local)
    assert b2 is not None and (b2.provider, b2.config_revision) == ("gemini", on_gemini.local)
    assert t2.ma_session_id != t1.ma_session_id


async def test_the_identity_survives_a_restart_and_owns_the_journal(
    providers: dict[str, _Provider],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session)
    revision = await set_channel_backend(
        db_session, channel_ref(world.tenant.id, "slack", "chan-x"), CONFIGS[OPENAI]
    )
    await db_session.commit()
    first = await _bind(_deps(db_session_factory), world, world.alice, revision, user="alice")

    # A new process: fresh deps, the same database. The thread resolves the same.
    again = await _bind(_deps(db_session_factory), world, world.bob, revision, user="bob")
    assert again.ma_session_id == first.ma_session_id
    assert again.session_account_id == first.session_account_id == shared_owner(_slot(world))

    store = PostgresStateStore(db_session_factory, trust_caller_clock=True)
    now = datetime.now(UTC)
    shared_lease = await store.acquire_lease(
        _slot(world), holder="w", turn_id="t", now=now, ttl=timedelta(minutes=1)
    )
    event = Event(
        id="ev_1",
        session_id=first.ma_session_id,
        sequence=0,
        type="agent.message",
        observed_at=now,
        authority="record",
        payload={"item_id": "item_1", "content": []},
        native=NativeProvenance(provider="openai", api_revision="offline"),
    )
    session_ref = ResourceRef(
        id=first.ma_session_id, kind="session", provider="openai", account_scope_id="openai-project"
    )
    appended = await store.append_events(
        session_ref,
        [JournalEntry(source_key="item_1", event=event)],
        fence=shared_lease,
        cursor="c1",
        now=now,
    )
    assert [e.sequence for e in appended.appended] == [0]
    # A caller's private lease cannot write the shared thread's journal.
    private = Slot(thread=_slot(world).thread, account_id=str(world.alice))
    private_lease = await store.acquire_lease(
        private, holder="w", turn_id="t", now=now, ttl=timedelta(minutes=1)
    )
    with pytest.raises(ScopeViolation):
        await store.append_events(
            session_ref,
            [JournalEntry(source_key="item_2", event=event.model_copy(update={"id": "ev_2"}))],
            fence=private_lease,
            cursor="c2",
            now=now,
        )


async def test_an_unconfigured_channel_never_reaches_a_provider_or_the_shared_slot(
    providers: dict[str, _Provider],
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session)
    seen: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    async def legacy_bind(_deps: TurnDeps, admission: Admission, **kwargs: Any) -> object:
        seen.append((kwargs["session_account_id"], admission.shared_owner))
        return object()

    async def no_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an unconfigured turn must not read a shared binding")

    monkeypatch.setattr(preparation, "bind_session_impl", legacy_bind)
    monkeypatch.setattr(preparation, "resolve_shared_thread", no_resolution)
    deps = _deps(db_session_factory)
    for account, user in ((world.alice, "alice"), (world.bob, "bob")):
        # The stand-in result has no session id; the decision is already recorded.
        with contextlib.suppress(AttributeError):
            await _bind(deps, world, account, None, user=user)

    assert seen == [(world.alice, None), (world.bob, None)]
    assert all(not p.calls for p in providers.values())
    async with db_session_factory() as s:
        assert await mux_state.get_binding(s, _slot(world)) is None


# PENDING until G1 (OpenAI) / G2 (Gemini) host codecs merge and register.


@pytest.mark.parametrize(("profile", "branch"), [(OPENAI, "G1"), (GEMINI, "G2")])
async def test_the_real_codec_receives_the_shared_owner(
    profile: str,
    branch: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registered: Callable[..., Any] | None = preparation._PROVIDER_PREPARATIONS.get(profile)  # pyright: ignore[reportPrivateUsage]
    if registered is None or profile not in channel_backend.RUNNABLE_PROFILES:
        pytest.skip(f"PENDING[{branch}]: no host preparation registered/runnable for {profile}")
    seen: list[uuid.UUID] = []

    class _Stop(Exception):
        pass

    async def spy(request: ProviderPreparationRequest) -> PreparedTurn:
        seen.append(request.session_account_id)
        raise _Stop  # stop before any native I/O: only the identity handed over is checked

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, profile, spy)  # pyright: ignore[reportPrivateUsage]
    world = await _world(db_session)
    revision = await set_channel_backend(
        db_session, channel_ref(world.tenant.id, "slack", "chan-x"), CONFIGS[profile]
    )
    await db_session.commit()
    with pytest.raises(_Stop):
        await _bind(_deps(db_session_factory), world, world.alice, revision, user="alice")
    assert seen == [shared_owner(_slot(world))]
