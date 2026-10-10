"""Authoritative foreign root failures survive display compatibility and host storage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsSessionRetriesExhausted
from daimon.core.turn import driver
from daimon.core.turn.gemini import GeminiTurnIO
from daimon.core.turn.io import TurnEvent
from daimon.core.turn.openai_codec import display_event
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_tenant
from daimon.testing.ma_transport import ScriptedTransport
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef, Scope
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import make_agent_message, make_status_idle
from .test_provider_dispatch import ACCOUNT, TENANT

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
    """Script normalized input; use production display codecs and the host pump."""

    def __init__(self, event: Event, leading=()):
        self.event = event
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
        self.frames = [*leading, self.frame]

    async def open_stream(self, **kwargs):
        return self

    async def send(self, events):
        self.sent = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        assert self.sent, "host must send before consuming the terminal record"
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def close(self):
        pass


@pytest.fixture
async def host(db_engine: AsyncEngine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm.begin() as db:
        await make_tenant(db, id=TENANT)
    return sm


async def run_host(monkeypatch, event, leading=()):
    io = OfflineIO(event, leading)
    monkeypatch.setattr(driver, "turn_io", lambda *args, **kwargs: io)
    transport = ScriptedTransport()
    lifecycle = RecordingLifecycle()
    async with transport.client() as client:
        state = await driver.run_turn(
            anthropic=client,
            session_id=event.session_id,
            user_message="offline",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="headless-unrecorded"),
            path="mux",
            scope=SCOPE,
            render_interval_s=3600,
        )
    assert not transport.requests
    if state.error is not None:
        assert lifecycle.terminal_failures == [(state, state.error)]
        assert not lifecycle.terminal_success
    else:
        assert lifecycle.terminal_success == [state]
        assert not lifecycle.terminal_failures
    return state


async def outcome_row(host, native_session, run):
    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import TurnObservation, drain_outcomes

    observation = TurnObservation(
        host, TENANT, "slack", account_id=ACCOUNT, session_id=native_session.id
    )
    with observation.activate():
        state = await run()
        observation.finish(state=state)
    await drain_outcomes()
    async with host() as db:
        rows = await list_for_tenant(db, TENANT)
    matches = [row for row in rows if row.id == observation.id]
    assert len(matches) == 1
    row = matches[0]
    assert row.account_id == ACCOUNT and row.session_id == native_session.id
    return state, row


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("completed", TerminationReason.COMPLETED),
        ("interrupted", TerminationReason.INTERRUPTED),
        ("errored", TerminationReason.UPSTREAM),
        ("terminated", TerminationReason.SESSION_TERMINATED),
    ],
)
async def test_provider_root_outcome_reaches_terminal_hook_and_committed_row(
    host, monkeypatch, provider, outcome, expected
):
    async def run():
        return await run_host(monkeypatch, terminal(provider, outcome))

    state, row = await outcome_row(host, session(provider), run)
    assert state.termination == row.reason == expected
    assert (state.error is not None) == expected.is_failure
    assert row.error_class == ("TurnError" if expected.is_failure else None)


async def test_anthropic_retries_exhausted_keeps_legacy_completed_row(host, monkeypatch):
    async def run():
        return await run_host(monkeypatch, terminal("anthropic", "errored"))

    state, row = await outcome_row(host, session("anthropic"), run)
    assert state.termination == row.reason == TerminationReason.COMPLETED
    assert state.error is None and row.error_class is None


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize(
    "fields", [{"authority": "preview"}, {"authority": "gap"}, {"thread_id": "child"}]
)
async def test_non_authoritative_or_child_failure_cannot_replace_root_completion(
    host, monkeypatch, provider, fields
):
    # Inject a nonterminal display frame with untrusted normalized metadata.
    # The host must not promote it to a root failure before the real root ends.
    noise = TurnEvent(
        make_agent_message(event_id="partial", text="partial"),
        terminal(provider, "errored").model_copy(update=fields),
    )

    async def run():
        return await run_host(monkeypatch, terminal(provider, "completed"), [noise])

    state, row = await outcome_row(host, session(provider), run)
    assert state.termination == row.reason == TerminationReason.COMPLETED
    assert state.error is None
