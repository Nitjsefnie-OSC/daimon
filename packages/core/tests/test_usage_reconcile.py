"""Tests for daimon.core.usage_reconcile -- debiting model calls no live stream metered.

The prod shape (#17): a deploy restarted the worker streaming a turn; the MA
session kept making model calls server-side with no recorder attached, and the
next turn on the session only billed its own calls. `reconcile_unbilled` lists
the session's `span.model_request_end` events and debits the ones the ledger
does not hold yet, through the turn's own recorder.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import structlog.testing
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core._models import TenantLedger, UsageEvent
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import DeploymentDefault
from daimon.core.tenant_balance import debit_amount
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.posture import UsageRecorder
from daimon.core.turn.prepare import bind_recorder
from daimon.core.usage_reconcile import reconcile_unbilled
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_model_usage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 11, 9, 0, 0, tzinfo=UTC)
MODEL = "claude-sonnet-4-6"
MARKUP = Decimal("1.1")
SESSION = "sesn_gap"


def _request_end(event_id: str, *, input_tokens: int = 1000, output_tokens: int = 500) -> Any:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        is_error=False,
        model_request_start_id=f"start_{event_id}",
        model_usage=ma_model_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=200,
        ),
        processed_at=NOW,
        type="span.model_request_end",
    )


def _paged_router(events: list[Any], *, page_size: int, reads: list[str] | None = None) -> MARouter:
    """events.list serving `events` newest-first in pages of `page_size`."""
    dumped = [e.model_dump(mode="json") for e in reversed(events)]
    router = MARouter()

    def _list(request: httpx.Request, _match: object) -> httpx.Response:
        if reads is not None:
            reads.append(str(request.url))
        start = int(request.url.params.get("page") or 0)
        end = start + page_size
        return httpx.Response(
            200,
            json={
                "data": dumped[start:end],
                "next_page": str(end) if end < len(dumped) else None,
            },
        )

    router.add("GET", r"/v1/sessions/[^/]+/events", _list)
    return router


def _deps(sessionmaker: async_sessionmaker[AsyncSession], router: MARouter) -> TurnDeps:
    return TurnDeps(
        anthropic=build_fake_anthropic(router.dispatch),
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


def _recorder(deps: TurnDeps, tenant_id: uuid.UUID) -> UsageRecorder:
    """The recorder the live turn path binds for this session."""
    return bind_recorder(
        deps,
        tenant_id=tenant_id,
        external_user_id="user-1",
        ma_session_id=SESSION,
        model_id=MODEL,
        channel_id="chan-1",
    )


async def _ledger(db_session: AsyncSession) -> dict[str, Decimal]:
    rows = (await db_session.execute(select(TenantLedger))).scalars().all()
    return {r.idempotency_key: r.delta_usd for r in rows}


async def test_gap_requests_are_debited_with_the_turn_key(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end(f"evt_{i}") for i in range(5)]
    deps = _deps(db_session_factory, _paged_router(events, page_size=2))

    with structlog.testing.capture_logs() as logs:
        result = await reconcile_unbilled(
            anthropic=deps.anthropic,
            sessionmaker=db_session_factory,
            managed_session_id=SESSION,
            record=_recorder(deps, tenant.id),
            trigger="attach",
        )

    ledger = await _ledger(db_session)
    assert set(ledger) == {f"turn:{SESSION}:evt_{i}" for i in range(5)}, (
        "every unbilled call across all pages gets the live path's idempotency key"
    )
    assert result.debited == 5 and result.scanned == 5 and not result.truncated
    assert result.debit_usd == -sum(ledger.values()), "logged USD is what the ledger holds"
    reconciled = [e for e in logs if e["event"] == "billing.reconciled_unbilled"]
    assert len(reconciled) == 1, "one structured line per pass that debits"
    assert reconciled[0]["debited"] == 5
    assert reconciled[0]["trigger"] == "attach"
    assert reconciled[0]["debit_usd"] == str(result.debit_usd)
    rows = (await db_session.execute(select(TenantLedger))).scalars().all()
    assert {r.reason for r in rows} == {"turn_debit"}
    assert {r.channel_id for r in rows} == {"chan-1"}, "budget channel attribution is kept"


async def test_reconciling_twice_never_double_debits(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end(f"evt_{i}") for i in range(3)]
    deps = _deps(db_session_factory, _paged_router(events, page_size=100))
    record = _recorder(deps, tenant.id)

    first = await reconcile_unbilled(
        anthropic=deps.anthropic,
        sessionmaker=db_session_factory,
        managed_session_id=SESSION,
        record=record,
        trigger="attach",
    )
    balance_after_first = sum((await _ledger(db_session)).values())
    second = await reconcile_unbilled(
        anthropic=deps.anthropic,
        sessionmaker=db_session_factory,
        managed_session_id=SESSION,
        record=record,
        trigger="turn_end",
    )

    assert first.debited == 3
    assert second.debited == 0 and second.debit_usd == 0, "second pass finds nothing to bill"
    ledger = await _ledger(db_session)
    assert len(ledger) == 3, "one ledger row per model call"
    assert sum(ledger.values()) == balance_after_first


async def test_requests_already_debited_live_are_skipped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end(f"evt_{i}") for i in range(4)]
    deps = _deps(db_session_factory, _paged_router(events, page_size=100))
    live = _recorder(deps, tenant.id)
    # The live stream metered the first two before the worker went away.
    await live(event=events[0])
    await live(event=events[1])

    handed: list[str] = []

    async def counting(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        handed.append(event.id)
        await live(event=event)

    result = await reconcile_unbilled(
        anthropic=deps.anthropic,
        sessionmaker=db_session_factory,
        managed_session_id=SESSION,
        record=counting,
        trigger="attach",
    )

    assert sorted(handed) == ["evt_2", "evt_3"], "metered calls never reach the recorder again"
    assert result.scanned == 4 and result.debited == 2
    assert len(await _ledger(db_session)) == 4
    usage_rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(usage_rows) == 4


async def test_reconciled_price_matches_the_live_path(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    live_event = _request_end("evt_live", input_tokens=12_345, output_tokens=678)
    gap_event = _request_end("evt_gap", input_tokens=12_345, output_tokens=678)
    deps = _deps(db_session_factory, _paged_router([live_event, gap_event], page_size=100))
    record = _recorder(deps, tenant.id)
    await record(event=live_event)

    await reconcile_unbilled(
        anthropic=deps.anthropic,
        sessionmaker=db_session_factory,
        managed_session_id=SESSION,
        record=record,
        trigger="attach",
    )

    ledger = await _ledger(db_session)
    live = ledger[f"turn:{SESSION}:evt_live"]
    gap = ledger[f"turn:{SESSION}:evt_gap"]
    expected = -debit_amount(cost_of(gap_event.model_usage, MODEL_PRICING[MODEL]), markup=MARKUP)
    assert gap == live, "a backfilled call costs exactly what the live stream would charge"
    assert gap == expected, "priced at the session model with the deployment markup"
    assert gap < 0


async def test_page_cap_bounds_the_read_newest_first(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end(f"evt_{i:02d}") for i in range(10)]
    reads: list[str] = []
    deps = _deps(db_session_factory, _paged_router(events, page_size=3, reads=reads))

    with structlog.testing.capture_logs() as logs:
        result = await reconcile_unbilled(
            anthropic=deps.anthropic,
            sessionmaker=db_session_factory,
            managed_session_id=SESSION,
            record=_recorder(deps, tenant.id),
            trigger="turn_end",
            page_size=3,
            max_pages=2,
        )

    assert len(reads) == 2, "never reads past the page cap"
    assert "order=desc" in reads[0] and "span.model_request_end" in reads[0]
    assert result.truncated and result.debited == 6
    assert set(await _ledger(db_session)) == {
        f"turn:{SESSION}:evt_{i:02d}" for i in range(4, 10)
    }, "the most recent calls are covered first"
    assert [e["truncated"] for e in logs if e["event"] == "billing.reconciled_unbilled"] == [True]


async def test_nothing_to_bill_reads_one_page_and_stays_quiet(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    router = MARouter()
    router.add("GET", r"/v1/sessions/[^/]+/events", lambda _r, _m: list_response([]))
    deps = _deps(db_session_factory, router)

    with structlog.testing.capture_logs() as logs:
        result = await reconcile_unbilled(
            anthropic=deps.anthropic,
            sessionmaker=db_session_factory,
            managed_session_id=SESSION,
            record=_recorder(deps, tenant.id),
            trigger="turn_end",
        )

    assert result.debited == 0 and not result.truncated
    assert not [e for e in logs if e["event"] == "billing.reconciled_unbilled"]
