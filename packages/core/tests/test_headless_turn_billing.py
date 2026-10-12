"""Tests for daimon.core.headless_turn_billing -- billing turns no stream drives.

The prod shape (#19): an MCP `start_turn` sends a message and returns; nothing
meters the session's model calls, the sweep is off, and the turn outcome was
written at dispatch as `unknown` with no usage. `settle_turn` waits for the
turn to end, debits the session's unbilled calls through the turn-end
catch-up, and writes the outcome with the real reason and usage.
"""

from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
    BetaManagedAgentsTextBlock,
    BetaManagedAgentsUserMessageEvent,
)
from daimon.core import headless_turn_billing
from daimon.core._models import TenantLedger, TurnOutcome, UsageEvent
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.headless_turn_billing import DispatchedTurn, settle_turn
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes, observe_turn
from daimon.core.turn.prepare import bind_recorder
from daimon.core.usage_reconcile import ReconcileResult
from daimon.core.usage_sweep import sweep_headless_usage
from daimon.testing.factories import make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic
from daimon.testing.ma_models import ma_model_usage, ma_session
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
MODEL = "claude-sonnet-4-6"
MARKUP = Decimal("1.1")
SESSION = "sesn_headless"
BOUNDARY = "evt_boundary"


def _request_end(event_id: str, *, at: datetime, input_tokens: int = 1000) -> dict[str, object]:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        is_error=False,
        model_request_start_id=f"start_{event_id}",
        model_usage=ma_model_usage(
            input_tokens=input_tokens, output_tokens=500, cache_read_input_tokens=200
        ),
        processed_at=at,
        type="span.model_request_end",
    ).model_dump(mode="json")


def _user_message(event_id: str, *, at: datetime) -> dict[str, object]:
    return BetaManagedAgentsUserMessageEvent(
        id=event_id,
        content=[BetaManagedAgentsTextBlock(type="text", text="hi")],
        type="user.message",
        processed_at=at,
    ).model_dump(mode="json")


def _idle(event_id: str, *, at: datetime) -> dict[str, object]:
    return BetaManagedAgentsSessionStatusIdleEvent(
        id=event_id,
        processed_at=at,
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        type="session.status_idle",
    ).model_dump(mode="json")


def _session(
    tenant_id: uuid.UUID, *, status: str = "idle", exempt: bool = False
) -> BetaManagedAgentsSession:
    metadata = {MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_BUDGET_CHANNEL: "chan-1"}
    if exempt:
        metadata[MA_METADATA_KEY_BILLING_EXEMPT] = "mcp-internal-caller"
    return ma_session(id=SESSION, model=MODEL, status=status, metadata=metadata)  # type: ignore[arg-type]


def _finished_turn(state: FakeSessionsState, *calls: str) -> None:
    """A turn MA ran to the end: boundary message, its model calls, then idle."""
    events = state.events.setdefault(SESSION, [])
    events.append(_user_message(BOUNDARY, at=NOW))
    for i, call in enumerate(calls, start=1):
        events.append(_request_end(call, at=NOW + timedelta(seconds=i), input_tokens=1000 * i))
    events.append(_idle("evt_idle", at=NOW + timedelta(seconds=len(calls) + 1)))


def _fake(session: BetaManagedAgentsSession) -> tuple[FakeSessionsState, object]:
    state = FakeSessionsState(ma=FakeMAState())
    state.sessions[session.id] = session
    return state, build_fake_anthropic(make_fake_sessions_handler(state))


def _turn(session: BetaManagedAgentsSession, tenant_id: uuid.UUID) -> DispatchedTurn:
    return DispatchedTurn(
        session=session, tenant_id=tenant_id, platform_user_id="user-1", turn_event_id=BOUNDARY
    )


@pytest.fixture(autouse=True)
async def _fresh_process_state() -> AsyncIterator[None]:
    """Watchers, queued catch-ups and the 429 cooldown are per process."""
    await headless_turn_billing.shutdown()
    headless_turn_billing._last_catch_up.clear()  # pyright: ignore[reportPrivateUsage]
    headless_turn_billing._unsettled.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    await headless_turn_billing.shutdown()
    await drain_outcomes()


async def _no_sleep(_s: float) -> None:
    raise AssertionError("a finished turn is settled without waiting")


async def _ledger(db_session: AsyncSession) -> dict[str, TenantLedger]:
    rows = (await db_session.execute(select(TenantLedger))).scalars().all()
    return {r.idempotency_key: r for r in rows}


def _chat_deps(sessionmaker: async_sessionmaker[AsyncSession], client: object) -> TurnDeps:
    return TurnDeps(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=MARKUP,
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )


async def test_a_finished_headless_turn_is_debited_once_at_the_chat_path_price(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    _finished_turn(state, "evt_a", "evt_b")

    await settle_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        sleep=_no_sleep,
    )

    ledger = await _ledger(db_session)
    assert set(ledger) == {f"turn:{SESSION}:evt_a", f"turn:{SESSION}:evt_b"}, (
        "every model call of the turn is debited under the chat path's key"
    )
    assert {r.reason for r in ledger.values()} == {"turn_debit"}
    assert {r.channel_id for r in ledger.values()} == {"chan-1"}, "the session's budget channel"

    # The chat path's own recorder, on another session, prices the same calls.
    chat = bind_recorder(
        _chat_deps(db_session_factory, client),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sesn_chat",
        model_id=MODEL,
        channel_id="chan-1",
    )
    for event in state.events[SESSION]:
        if event["type"] == "span.model_request_end":
            await chat(event=BetaManagedAgentsSpanModelRequestEndEvent.model_validate(event))
    ledger = await _ledger(db_session)
    for call in ("evt_a", "evt_b"):
        headless = ledger[f"turn:{SESSION}:{call}"].delta_usd
        assert headless == ledger[f"turn:sesn_chat:{call}"].delta_usd, "same price and markup"
        assert headless < 0
    usage = (
        (
            await db_session.execute(
                select(UsageEvent).where(UsageEvent.managed_session_id == SESSION)
            )
        )
        .scalars()
        .all()
    )
    assert {(u.platform_user_id, u.channel_id, u.model) for u in usage} == {
        ("user-1", "chan-1", MODEL)
    }


async def test_settling_again_or_sweeping_never_double_debits(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    _finished_turn(state, "evt_a", "evt_b")
    kwargs = {
        "anthropic": client,
        "sessionmaker": db_session_factory,
        "turn": _turn(session, tenant.id),
        "markup": MARKUP,
        "sleep": _no_sleep,
    }

    await settle_turn(**kwargs)  # type: ignore[arg-type]
    first = {k: r.delta_usd for k, r in (await _ledger(db_session)).items()}
    await settle_turn(**kwargs)  # type: ignore[arg-type]
    swept = await sweep_headless_usage(client, db_session_factory, markup=MARKUP)  # type: ignore[arg-type]

    assert swept == 0, "the sweep finds every call already recorded"
    assert {k: r.delta_usd for k, r in (await _ledger(db_session)).items()} == first


async def test_a_stale_idle_from_the_previous_turn_is_waited_out(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Right after a send the session can still read idle from the previous turn."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    events = state.events.setdefault(SESSION, [])
    events.append(_idle("evt_prev_idle", at=NOW - timedelta(minutes=1)))
    events.append(_user_message(BOUNDARY, at=NOW))
    sleeps: list[float] = []

    async def agent_finishes_while_we_sleep(s: float) -> None:
        sleeps.append(s)
        events.append(_request_end("evt_late", at=NOW + timedelta(seconds=5)))
        events.append(_idle("evt_idle", at=NOW + timedelta(seconds=6)))

    observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    observation.usage_available = False
    await settle_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        observation=observation,
        sleep=agent_finishes_while_we_sleep,
    )
    await drain_outcomes()

    assert sleeps == [headless_turn_billing.POLL_INITIAL_S], "one wait, then the real end"
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_late"}
    row = (await db_session.execute(select(TurnOutcome))).scalar_one()
    assert (row.reason, row.model_calls, row.billing_posture) == ("completed", 1, "metered")


async def test_the_outcome_carries_the_turns_reason_and_cost(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    # An earlier turn's call is billed by the catch-up but is not this turn's cost.
    state.events[SESSION] = [_request_end("evt_old", at=NOW - timedelta(minutes=5))]
    _finished_turn(state, "evt_a", "evt_b")
    observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    observation.usage_available = False

    await settle_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        observation=observation,
        sleep=_no_sleep,
    )
    await drain_outcomes()

    assert len(await _ledger(db_session)) == 3, "the earlier unbilled call is caught up too"
    row = (await db_session.execute(select(TurnOutcome))).scalar_one()
    pricing = MODEL_PRICING[MODEL]
    expected = sum(
        (
            Decimal(
                str(
                    cost_of(
                        ma_model_usage(
                            input_tokens=n, output_tokens=500, cache_read_input_tokens=200
                        ),
                        pricing,
                    )
                )
            )
            for n in (1000, 2000)
        ),
        Decimal(0),
    )
    assert row.reason == "completed"
    assert row.session_id == SESSION
    assert row.model_calls == 2, "only the calls after the turn's own message"
    assert row.input_tokens == 3000
    assert row.cost_usd is not None
    assert abs(row.cost_usd - expected) < Decimal("1e-9"), "pre-markup cost of the two calls"
    assert row.billing_posture == "metered"


async def test_an_exempt_session_is_not_debited(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id, exempt=True)
    state, client = _fake(session)
    _finished_turn(state, "evt_a")
    observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)

    await settle_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        observation=observation,
        sleep=_no_sleep,
    )
    await drain_outcomes()

    assert await _ledger(db_session) == {}
    row = (await db_session.execute(select(TurnOutcome))).scalar_one()
    assert (row.model_calls, row.billing_posture) == (1, "exempt")


async def test_a_wait_that_runs_out_still_bills_and_records_unknown(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id, status="running")
    state, client = _fake(session)
    state.events[SESSION] = [
        _user_message(BOUNDARY, at=NOW),
        _request_end("evt_so_far", at=NOW + timedelta(seconds=1)),
    ]
    observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    observation.usage_available = False
    ticks = itertools.chain([0.0], itertools.repeat(10.0))

    await settle_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        observation=observation,
        timeout_s=5.0,
        sleep=_no_sleep,
        clock=lambda: next(ticks),
    )
    await drain_outcomes()

    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_so_far"}
    row = (await db_session.execute(select(TurnOutcome))).scalar_one()
    assert (row.reason, row.model_calls) == ("unknown", None)


@pytest.mark.parametrize("status", ["running", "idle"])
async def test_a_poll_catch_up_is_throttled_and_skips_running_sessions(
    status: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id, status=status)
    state, client = _fake(session)
    _finished_turn(state, "evt_a")
    clock = iter([100.0, 101.0, 100.0 + headless_turn_billing.CATCH_UP_INTERVAL_S + 1])

    def poll() -> bool:
        return headless_turn_billing.catch_up_session(
            anthropic=client,  # type: ignore[arg-type]
            sessionmaker=db_session_factory,
            session=session,
            tenant_id=tenant.id,
            platform_user_id="user-1",
            markup=MARKUP,
            clock=lambda: next(clock),
        )

    spawned: list[bool] = []
    for _ in range(3):
        spawned.append(poll())
        await headless_turn_billing.drain()

    if status == "running":
        assert spawned == [False, False, False]
        assert await _ledger(db_session) == {}
    else:
        assert spawned == [True, False, True], "at most one pass per interval"
        assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}


# ---------------------------------------------------------------------------
# Review regressions (#865): settlement retries, turn intervals, deadlines,
# shutdown, and background read pacing.
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(headless_turn_billing, "SETTLE_RETRY_INITIAL_S", 0.01)
    monkeypatch.setattr(headless_turn_billing, "SETTLE_CONTINUE_S", 0.0)


async def test_a_failed_terminal_pass_is_retried_even_after_the_final_poll(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fast_retries: None,
) -> None:
    """One transient DB failure while settling must not leave a finished turn unbilled,
    though the host's last poll was suppressed by the pending watcher."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    _finished_turn(state, "evt_a")
    actual = headless_turn_billing.reconcile_unbilled
    attempts = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_once(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
            raise SQLAlchemyError("injected transient failure")
        return await actual(**kwargs)

    monkeypatch.setattr(headless_turn_billing, "reconcile_unbilled", fail_once)
    observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    observation.usage_available = False
    headless_turn_billing.watch_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        observation=observation,
    )
    await entered.wait()
    final_poll = headless_turn_billing.catch_up_session(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        session=session,
        tenant_id=tenant.id,
        platform_user_id="user-1",
        markup=MARKUP,
    )
    release.set()
    await headless_turn_billing.drain()
    await drain_outcomes()

    assert final_poll is False, "the pending watcher owns the session"
    assert attempts == 2, "the failed pass is retried"
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}
    row = (await db_session.execute(select(TurnOutcome))).scalar_one()
    assert (row.reason, row.billing_posture) == ("completed", "metered")


async def test_a_truncated_terminal_pass_is_continued(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fast_retries: None,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    _finished_turn(state, "evt_a", "evt_b")
    actual = headless_turn_billing.reconcile_unbilled
    truncated: list[bool] = []

    async def one_call_per_pass(**kwargs: Any) -> Any:
        result = await actual(**kwargs, page_size=1, max_pages=1)
        truncated.append(result.truncated)
        return result

    monkeypatch.setattr(headless_turn_billing, "reconcile_unbilled", one_call_per_pass)
    headless_turn_billing.watch_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
    )
    await headless_turn_billing.drain()

    assert truncated[0] is True and truncated[-1] is False
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a", f"turn:{SESSION}:evt_b"}


async def test_an_abandoned_settlement_is_left_to_the_poll_fallback(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fast_retries: None,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    _finished_turn(state, "evt_a")
    actual = headless_turn_billing.reconcile_unbilled
    down = [True]

    async def database_down(**kwargs: Any) -> Any:
        if down[0]:
            raise SQLAlchemyError("down")
        return await actual(**kwargs)

    monkeypatch.setattr(headless_turn_billing, "SETTLE_MAX_PASSES", 2)
    monkeypatch.setattr(headless_turn_billing, "reconcile_unbilled", database_down)
    headless_turn_billing.watch_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
    )
    await headless_turn_billing.drain()
    assert await _ledger(db_session) == {}

    down[0] = False
    # Inside the normal 30 s throttle: an unsettled session is retried sooner.
    ticks = iter([1000.0, 1000.0 + headless_turn_billing.CATCH_UP_RETRY_S + 1])
    for _ in range(2):
        headless_turn_billing.catch_up_session(
            anthropic=client,  # type: ignore[arg-type]
            sessionmaker=db_session_factory,
            session=session,
            tenant_id=tenant.id,
            platform_user_id="user-1",
            markup=MARKUP,
            clock=lambda: next(ticks),
        )
        await headless_turn_billing.drain()

    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}


async def test_a_delayed_watcher_counts_only_its_own_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Turn B was sent and finished before A's watcher looked: A's outcome is A's."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    _finished_turn(state, "evt_a")
    state.events[SESSION].extend(
        [
            _user_message("evt_boundary_b", at=NOW + timedelta(seconds=3)),
            _request_end("evt_b", at=NOW + timedelta(seconds=4)),
            _idle("evt_idle_b", at=NOW + timedelta(seconds=5)),
        ]
    )
    observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    observation.usage_available = False

    await settle_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        observation=observation,
        sleep=_no_sleep,
    )
    await drain_outcomes()

    row = (await db_session.execute(select(TurnOutcome))).scalar_one()
    assert row.model_calls == 1, "turn B's call is not turn A's cost"
    assert [ref["event_id"] for ref in row.usage_refs] == ["evt_a"]
    assert len(await _ledger(db_session)) == 2, "the session is still billed in full"


async def test_turns_sharing_a_session_get_their_own_outcomes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    _finished_turn(state, "evt_a")
    state.events[SESSION].extend(
        [
            _user_message("evt_boundary_b", at=NOW + timedelta(seconds=3)),
            _request_end("evt_b1", at=NOW + timedelta(seconds=4)),
            _request_end("evt_b2", at=NOW + timedelta(seconds=5)),
            _idle("evt_idle_b", at=NOW + timedelta(seconds=6)),
        ]
    )
    observations = []
    for boundary in (BOUNDARY, "evt_boundary_b"):
        observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
        observation.usage_available = False
        observations.append(observation)
        headless_turn_billing.watch_turn(
            anthropic=client,  # type: ignore[arg-type]
            sessionmaker=db_session_factory,
            turn=DispatchedTurn(
                session=session,
                tenant_id=tenant.id,
                platform_user_id="user-1",
                turn_event_id=boundary,
            ),
            markup=MARKUP,
            observation=observation,
        )
    await headless_turn_billing.drain()
    await drain_outcomes()

    rows = (await db_session.execute(select(TurnOutcome))).scalars().all()
    by_id = {r.id: r.model_calls for r in rows}
    assert by_id == {observations[0].id: 1, observations[1].id: 2}
    assert len(await _ledger(db_session)) == 3


def _slow_client(session: BetaManagedAgentsSession, delay_s: float) -> Any:
    """Every MA read takes `delay_s`: a wedged upstream."""
    state = FakeSessionsState(ma=FakeMAState())
    state.sessions[session.id] = session
    inner = make_fake_sessions_handler(state)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay_s)
        return inner(request)

    return build_fake_anthropic(handler)  # type: ignore[arg-type]


async def test_the_watcher_deadline_bounds_an_in_flight_read(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id, status="running")
    client = _slow_client(session, delay_s=5.0)
    observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    observation.usage_available = False
    # Billing afterwards is separately bounded; make it instant here.
    started = time.monotonic()
    with patch.object(
        headless_turn_billing, "_reconcile", AsyncMock(return_value=_settled_result())
    ):
        await asyncio.wait_for(
            settle_turn(
                anthropic=client,
                sessionmaker=db_session_factory,
                turn=_turn(session, tenant.id),
                markup=MARKUP,
                observation=observation,
                timeout_s=0.05,
            ),
            timeout=2.0,
        )
    await drain_outcomes()

    assert time.monotonic() - started < 1.0, "the 50 ms deadline cut the 5 s read short"
    row = (await db_session.execute(select(TurnOutcome))).scalar_one()
    assert row.reason == "unknown"


def _settled_result() -> ReconcileResult:
    return ReconcileResult(
        scanned=0,
        missing=0,
        repaired=0,
        debited=0,
        debit_usd=Decimal(0),
        truncated=False,
        repair_skipped=False,
    )


async def test_background_reads_use_their_own_timeout_and_retry_budget() -> None:
    client = build_fake_anthropic(lambda _r: httpx.Response(500))
    background = headless_turn_billing._background(client)  # pyright: ignore[reportPrivateUsage]
    assert background.max_retries == headless_turn_billing.BACKGROUND_MAX_RETRIES
    assert background.timeout == headless_turn_billing.REQUEST_TIMEOUT_S


async def test_a_rate_limited_status_read_backs_off_to_retry_after(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state = FakeSessionsState(ma=FakeMAState())
    state.sessions[session.id] = session
    _finished_turn(state, "evt_a")
    inner = make_fake_sessions_handler(state)
    limited = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        if limited[0] and request.url.path == f"/v1/sessions/{SESSION}":
            return httpx.Response(
                429,
                headers={"retry-after": "42"},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            )
        return inner(request)

    client = build_fake_anthropic(handler)
    sleeps: list[float] = []

    async def record_sleep(s: float) -> None:
        sleeps.append(s)
        limited[0] = False

    await settle_turn(
        anthropic=client,
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        sleep=record_sleep,
    )

    assert sleeps == [42.0], "the pause honours retry-after"
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}


async def test_an_unreachable_boundary_stops_being_walked(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    state.events[SESSION] = [_idle("evt_idle", at=NOW)]  # the boundary is never listed
    sleeps: list[float] = []

    async def count(s: float) -> None:
        sleeps.append(s)

    observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    observation.usage_available = False
    await settle_turn(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        observation=observation,
        sleep=count,
    )
    await drain_outcomes()

    assert len(sleeps) == headless_turn_billing.MAX_UNRESOLVED_WALKS - 1
    row = (await db_session.execute(select(TurnOutcome))).scalar_one()
    assert (row.reason, row.model_calls) == ("unknown", None)


async def test_shutdown_before_the_watcher_starts_still_writes_its_outcome(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id, status="running")
    _, client = _fake(session)
    with observe_turn(db_session_factory, tenant_id=tenant.id, platform="mcp") as observation:
        observation.usage_available = False
        observation.session_id = SESSION
        observation.hand_off()
        headless_turn_billing.watch_turn(
            anthropic=client,  # type: ignore[arg-type]
            sessionmaker=db_session_factory,
            turn=_turn(session, tenant.id),
            markup=MARKUP,
            observation=observation,
        )
    await headless_turn_billing.shutdown()  # no yield: the task never took a step
    await drain_outcomes()

    rows = (await db_session.execute(select(TurnOutcome))).scalars().all()
    assert [r.reason for r in rows] == ["unknown"]


async def test_watched_sessions_and_queued_catch_ups_are_capped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    monkeypatch.setattr(headless_turn_billing, "MAX_WATCHERS", 1)
    monkeypatch.setattr(headless_turn_billing, "MAX_CATCH_UPS", 1)
    gate = asyncio.Event()

    async def held(**_kwargs: Any) -> Any:
        await gate.wait()
        return _settled_result()

    monkeypatch.setattr(headless_turn_billing, "reconcile_unbilled", held)
    sessions = [
        ma_session(
            id=f"sesn_cap_{i}",
            model=MODEL,
            status="running" if i < 2 else "idle",
            metadata={MA_METADATA_KEY_TENANT: str(tenant.id)},
        )
        for i in range(4)
    ]
    state = FakeSessionsState(ma=FakeMAState())
    for s in sessions:
        state.sessions[s.id] = s
    client = build_fake_anthropic(make_fake_sessions_handler(state))
    outcomes = []
    for s in sessions[:2]:
        observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=s.id)
        outcomes.append(observation)
        headless_turn_billing.watch_turn(
            anthropic=client,
            sessionmaker=db_session_factory,
            turn=DispatchedTurn(
                session=s, tenant_id=tenant.id, platform_user_id=None, turn_event_id="b"
            ),
            markup=MARKUP,
            observation=observation,
        )
    queued = [
        headless_turn_billing.catch_up_session(
            anthropic=client,
            sessionmaker=db_session_factory,
            session=s,
            tenant_id=tenant.id,
            platform_user_id=None,
            markup=MARKUP,
        )
        for s in sessions[2:]
    ]

    assert outcomes[1].handed_off is False
    assert outcomes[1]._finished is True, "over the cap: written as unknown at once"  # pyright: ignore[reportPrivateUsage]
    assert queued == [True, False], "one queued catch-up at most"
    gate.set()
    await headless_turn_billing.shutdown()
    await drain_outcomes()


# ---------------------------------------------------------------------------
# Second review (#865): one shared 429 cooldown for every background path,
# fallback retries, and a bound on the turns one watcher holds.
# ---------------------------------------------------------------------------


def _limited() -> httpx.Response:
    return httpx.Response(
        429,
        headers={"retry-after": "42"},
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
    )


async def test_a_rate_limited_terminal_pass_waits_out_retry_after(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, _ = _fake(session)
    _finished_turn(state, "evt_a")
    inner = make_fake_sessions_handler(state)
    limited = [True]
    throttled = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        # The catch-up lists oldest first; the outcome walk newest first.
        if limited[0] and request.url.params.get("order") == "asc":
            throttled[0] += 1
            return _limited()
        return inner(request)

    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)
        limited[0] = False

    await settle_turn(
        anthropic=build_fake_anthropic(handler).with_options(max_retries=8),
        sessionmaker=db_session_factory,
        turn=_turn(session, tenant.id),
        markup=MARKUP,
        sleep=pause,
    )

    assert throttled[0] == 1, "no SDK retries under the background client"
    assert pauses and pauses[0] >= 42, "the next pass waits for retry-after, not 5 s"
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}


async def test_a_poll_during_the_cooldown_queues_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, _ = _fake(session)
    _finished_turn(state, "evt_a")
    inner = make_fake_sessions_handler(state)
    throttled = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("order") == "asc":
            throttled[0] += 1
            return _limited()
        return inner(request)

    client = build_fake_anthropic(handler).with_options(max_retries=8)

    def poll(at: float) -> bool:
        return headless_turn_billing.catch_up_session(
            anthropic=client,
            sessionmaker=db_session_factory,
            session=session,
            tenant_id=tenant.id,
            platform_user_id="user-1",
            markup=MARKUP,
            clock=lambda: at,
        )

    assert poll(1000.0)
    await headless_turn_billing.drain()
    assert not poll(1006.0), "an unsettled session's 5 s gap does not bypass the cooldown"
    await headless_turn_billing.drain()
    assert throttled[0] == 1


async def test_a_new_turn_does_not_cut_a_status_cooldown_short(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id, status="running")
    reads = [0]
    first = asyncio.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        reads[0] += 1
        first.set()
        return _limited()

    client = build_fake_anthropic(handler).with_options(max_retries=8)

    def dispatch(boundary: str) -> None:
        headless_turn_billing.watch_turn(
            anthropic=client,
            sessionmaker=db_session_factory,
            turn=DispatchedTurn(
                session=session,
                tenant_id=tenant.id,
                platform_user_id="user-1",
                turn_event_id=boundary,
            ),
            markup=MARKUP,
        )

    dispatch("evt_turn_a")
    await asyncio.wait_for(first.wait(), 1)
    await asyncio.sleep(0.02)
    dispatch("evt_turn_b")
    await asyncio.sleep(0.2)

    assert reads[0] == 1, "the second dispatch wakes polling, not the cooldown"


async def test_a_failed_fallback_pass_is_retried_in_the_background(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fast_retries: None,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    state, client = _fake(session)
    _finished_turn(state, "evt_a")
    actual = headless_turn_billing.reconcile_unbilled
    attempts = [0]

    async def fail_once(**kwargs: Any) -> Any:
        attempts[0] += 1
        if attempts[0] == 1:
            raise SQLAlchemyError("transient")
        return await actual(**kwargs)

    monkeypatch.setattr(headless_turn_billing, "reconcile_unbilled", fail_once)
    assert headless_turn_billing.catch_up_session(
        anthropic=client,  # type: ignore[arg-type]
        sessionmaker=db_session_factory,
        session=session,
        tenant_id=tenant.id,
        platform_user_id="user-1",
        markup=MARKUP,
    )
    await headless_turn_billing.drain()

    assert attempts[0] == 2, "retried without another poll"
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}


async def test_one_watcher_holds_a_bounded_number_of_turns(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    monkeypatch.setattr(headless_turn_billing, "MAX_PENDING_PER_SESSION", 2)
    session = _session(tenant.id, status="running")
    _, client = _fake(session)
    observations: list[TurnObservation] = []
    for i in range(3):
        observation = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
        observations.append(observation)
        headless_turn_billing.watch_turn(
            anthropic=client,  # type: ignore[arg-type]
            sessionmaker=db_session_factory,
            turn=DispatchedTurn(
                session=session,
                tenant_id=tenant.id,
                platform_user_id="user-1",
                turn_event_id=f"evt_turn_{i}",
            ),
            markup=MARKUP,
            observation=observation,
        )

    finished = [o._finished for o in observations]  # pyright: ignore[reportPrivateUsage]
    assert finished == [True, False, False], "the oldest turn is written unknown"
    await headless_turn_billing.shutdown()
    await drain_outcomes()


async def test_each_turn_expires_at_its_own_deadline(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stream of new sends does not keep an old turn's outcome open forever."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id, status="running")
    _, client = _fake(session)
    now = [0.0]
    watch = headless_turn_billing._Watch(  # pyright: ignore[reportPrivateUsage]
        session=session, record=None, deadline=0.0
    )
    old = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    new = TurnObservation(db_session_factory, tenant.id, "mcp", session_id=SESSION)
    watch.add(headless_turn_billing._PendingTurn("evt_old", old, deadline=10.0))  # pyright: ignore[reportPrivateUsage]
    watch.add(headless_turn_billing._PendingTurn("evt_new", new, deadline=100.0))  # pyright: ignore[reportPrivateUsage]
    now[0] = 50.0
    watch.expire(now[0])

    assert list(watch.pending) == ["evt_new"]
    assert old._finished and not new._finished  # pyright: ignore[reportPrivateUsage]
    assert watch.deadline == 100.0
    watch.finalize()
    await drain_outcomes()
    del client
