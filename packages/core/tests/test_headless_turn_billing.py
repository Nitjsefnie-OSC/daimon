"""Tests for daimon.core.headless_turn_billing -- billing turns no stream drives.

The prod shape (#19): an MCP `start_turn` sends a message and returns; nothing
meters the session's model calls, the sweep is off, and the turn outcome was
written at dispatch as `unknown` with no usage. `settle_turn` waits for the
turn to end, debits the session's unbilled calls through the turn-end
catch-up, and writes the outcome with the real reason and usage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.core.turn.prepare import bind_recorder
from daimon.core.usage_sweep import sweep_headless_usage
from daimon.testing.factories import make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic
from daimon.testing.ma_models import ma_model_usage, ma_session
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from sqlalchemy import select
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
    ticks = iter([0.0, 10.0])

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

    spawned = [poll(), poll(), poll()]
    await headless_turn_billing.drain()

    if status == "running":
        assert spawned == [False, False, False]
        assert await _ledger(db_session) == {}
    else:
        assert spawned == [True, False, True], "at most one pass per interval"
        assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}
