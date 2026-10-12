"""Headless agent-chat turns are debited once they end (#19).

`start_turn`/`continue_turn` send a message and return; no stream is driven,
so nothing meters the turn live. The tools hand each sent turn to a
background watcher that runs the turn-end catch-up when the session goes
idle, and `get_my_session` runs a throttled catch-up when it reads a
finished session. Exercised on the stateful sessions fake and a real ledger.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agent_chat import (
    _bill_when_finished,
    _catch_up_when_finished,
    _continue_turn_impl,
    _get_session_impl,
    _start_turn_impl,
)
from daimon.core import headless_turn_billing
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger, turn_outcomes
from daimon.core.stores.domain import TenantLedgerRow
from daimon.core.stores.turn_outcomes import OutcomeRecord
from daimon.core.tenant_balance import debit_amount
from daimon.core.turn.outcomes import drain_outcomes
from daimon.testing import ma_agent, ma_model_usage, ma_session
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    FakeMAState,
    MARouter,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
)
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MA_AGENT_ID = "ag_billing001"
_ENV_NAME = "production"
_MODEL = "claude-sonnet-4-6"
_MARKUP = Decimal("1.1")
_NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)


class _Agent:
    """Runs each sent turn to the end: `calls_per_turn` model calls, then idle."""

    def __init__(self, state: FakeSessionsState, *, calls_per_turn: int) -> None:
        self.state = state
        self.calls_per_turn = calls_per_turn
        self.ids = itertools.count(1)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/v1/sessions/([^/]+)/events", request.url.path)
        if request.method != "POST" or m is None:
            raise NotHandled
        response = make_fake_sessions_handler(self.state)(request)
        sid = m.group(1)
        events = self.state.events[sid]
        for _ in range(self.calls_per_turn):
            n = next(self.ids)
            events.append(
                BetaManagedAgentsSpanModelRequestEndEvent(
                    id=f"evt_call_{n}",
                    is_error=False,
                    model_request_start_id=f"start_{n}",
                    model_usage=ma_model_usage(
                        input_tokens=1000 * n, output_tokens=400, cache_read_input_tokens=100
                    ),
                    processed_at=_NOW + dt.timedelta(seconds=n),
                    type="span.model_request_end",
                ).model_dump(mode="json")
            )
        events.append(
            BetaManagedAgentsSessionStatusIdleEvent(
                id=f"evt_idle_{next(self.ids)}",
                processed_at=_NOW + dt.timedelta(minutes=1),
                stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
                type="session.status_idle",
            ).model_dump(mode="json")
        )
        self.state.sessions[sid] = self.state.sessions[sid].model_copy(update={"status": "idle"})
        return response


def _directory(tenant_id: uuid.UUID) -> MARouter:
    env = {
        "id": "env_billing001",
        "type": "environment",
        "name": _ENV_NAME,
        "config": EMPTY_CLOUD_CONFIG.model_dump(mode="json"),
        "description": "",
        "metadata": {"daimon_tenant": str(tenant_id), "daimon_name": _ENV_NAME},
        "created_at": "2026-06-23T00:00:00Z",
        "updated_at": "2026-06-23T00:00:00Z",
    }
    agent = ma_agent(
        id=_MA_AGENT_ID,
        name="test-agent",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "test-agent"},
    )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent.model_dump(mode="json")]))
    router.add("GET", r"/v1/environments", lambda _r, _m: list_response([env]))
    return router


class _World:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        *,
        calls_per_turn: int = 2,
    ) -> None:
        self.tenant_id = tenant_id
        self.state = FakeSessionsState(ma=FakeMAState())
        self.client: AsyncAnthropic = build_fake_anthropic(
            combine_handlers(
                _Agent(self.state, calls_per_turn=calls_per_turn),
                make_fake_sessions_handler(self.state),
                _directory(tenant_id).dispatch,
            )
        )
        settings = MagicMock()
        settings.mcp.jwt_secret = None
        settings.github.fallback_pat = None
        settings.billing.markup = _MARKUP
        self.runtime = McpRuntime(
            session_factory=sessionmaker,
            client=self.client,
            settings=settings,  # type: ignore[arg-type]
            deployment_default=DeploymentDefault(environment_name=_ENV_NAME),
        )
        self.auth = AuthIdentity(
            account_id=account_id,
            tenant_id=tenant_id,
            role=Role.USER,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID),
            platform="discord",
            platform_user_id="user-1",
        )
        # Unique per test: the poll throttle is per session and per process.
        self.sid = f"sesn_{uuid.uuid4().hex[:12]}"
        self.session: BetaManagedAgentsSession = ma_session(
            id=self.sid,
            agent_id=_MA_AGENT_ID,
            model=_MODEL,
            status="running",
            metadata={
                MA_METADATA_KEY_TENANT: str(tenant_id),
                MA_METADATA_KEY_ACCOUNT: str(account_id),
                MA_METADATA_KEY_BUDGET_CHANNEL: "chan-1",
            },
        )

    async def start(self) -> dict[str, str]:
        async def create(*_a: Any, **_k: Any) -> BetaManagedAgentsSession:
            self.state.sessions[self.session.id] = self.session
            return self.session

        with patch(
            "daimon.adapters.mcp.tools.agent_chat.create_session", new=AsyncMock(side_effect=create)
        ):
            started = await _start_turn_impl(
                self.runtime,
                self.auth,
                "hi",
                after_send=_bill_when_finished(self.runtime, self.auth, owns_outcome=True),
            )
        await self.settled()
        return started

    async def continue_(self) -> dict[str, str]:
        started = await _continue_turn_impl(
            self.runtime,
            self.auth,
            self.sid,
            "more",
            after_send=_bill_when_finished(self.runtime, self.auth, owns_outcome=True),
        )
        await self.settled()
        return started

    async def poll(self) -> str:
        info = await _get_session_impl(
            self.runtime,
            self.auth,
            self.sid,
            on_read=_catch_up_when_finished(self.runtime, self.auth),
        )
        await self.settled()
        return info.status

    async def settled(self) -> None:
        await headless_turn_billing.drain()
        await drain_outcomes()


@pytest.fixture
async def world(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> _World:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    return _World(db_session_factory, tenant.id, account.id)


async def _debits(world: _World, db_session: AsyncSession) -> list[TenantLedgerRow]:
    rows = await tenant_ledger.list_for_tenant(db_session, tenant_id=world.tenant_id)
    return [r for r in rows if r.reason == "turn_debit"]


async def _ledger(world: _World, db_session: AsyncSession) -> dict[str, Decimal]:
    return {r.idempotency_key: r.delta_usd for r in await _debits(world, db_session)}


async def _outcomes(world: _World, db_session: AsyncSession) -> list[OutcomeRecord]:
    return await turn_outcomes.list_for_tenant(db_session, world.tenant_id)


def _expected_debit(n: int) -> Decimal:
    """The chat path's price for the fake's n-th call (`record_turn_usage`)."""
    usage = ma_model_usage(input_tokens=1000 * n, output_tokens=400, cache_read_input_tokens=100)
    return -debit_amount(cost_of(usage, MODEL_PRICING[_MODEL]), markup=_MARKUP)


async def test_a_start_turn_is_debited_once_it_ends(
    world: _World, db_session: AsyncSession
) -> None:
    started = await world.start()

    ledger = await _ledger(world, db_session)
    assert ledger == {
        f"turn:{world.sid}:evt_call_1": _expected_debit(1),
        f"turn:{world.sid}:evt_call_2": _expected_debit(2),
    }, "each model call debited once, at the chat path's price, markup and key"
    assert {r.channel_id for r in await _debits(world, db_session)} == {"chan-1"}
    assert started["handle"] == world.sid

    outcomes = await _outcomes(world, db_session)
    assert len(outcomes) == 1, "the dispatch's outcome is written once, by the watcher"
    (outcome,) = outcomes
    assert (outcome.reason, outcome.session_id, outcome.platform) == (
        "completed",
        world.sid,
        "mcp",
    )
    assert outcome.model_calls == 2
    assert outcome.billing_posture == "metered"
    assert outcome.cost_usd is not None and outcome.cost_usd > 0


async def test_polling_a_finished_session_never_double_debits(
    world: _World, db_session: AsyncSession
) -> None:
    await world.start()
    billed = await _ledger(world, db_session)

    assert await world.poll() == "idle"
    assert await world.poll() == "idle"

    assert await _ledger(world, db_session) == billed


async def test_a_continued_turn_bills_only_its_new_calls(
    world: _World, db_session: AsyncSession
) -> None:
    await world.start()
    await world.continue_()

    ledger = await _ledger(world, db_session)
    assert set(ledger) == {f"turn:{world.sid}:evt_call_{n}" for n in (1, 2, 4, 5)}, (
        "two calls per turn, each once (the fake's idle events take ids 3 and 6)"
    )
    assert ledger[f"turn:{world.sid}:evt_call_5"] == _expected_debit(5)
    assert [o.reason for o in await _outcomes(world, db_session)] == ["completed", "completed"]


async def test_a_poll_catches_up_a_turn_no_watcher_saw(
    world: _World, db_session: AsyncSession
) -> None:
    """The process that sent the turn went away before it ended (a deploy)."""
    with patch("daimon.adapters.mcp.tools.agent_chat.watch_turn"):
        await world.start()
    assert await _ledger(world, db_session) == {}, "nothing billed the turn"

    assert await world.poll() == "idle"

    assert set(await _ledger(world, db_session)) == {
        f"turn:{world.sid}:evt_call_1",
        f"turn:{world.sid}:evt_call_2",
    }
