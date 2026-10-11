"""Tests for daimon.core.usage_reconcile -- debiting model calls no live stream metered.

The prod shape (#17): a deploy restarted the worker streaming a turn; the MA
session kept making model calls server-side with no recorder attached, and the
next turn on the session only billed its own calls. `reconcile_unbilled` lists
the session's `span.model_request_end` events and debits the ones the ledger
does not hold yet, through the turn's own recorder.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import structlog.testing
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core._models import TenantLedger, UsageEvent
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import usage_reconcile_progress
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


def _request_end(
    event_id: str,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    at: datetime = NOW,
) -> BetaManagedAgentsSpanModelRequestEndEvent:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        is_error=False,
        model_request_start_id=f"start_{event_id}",
        model_usage=ma_model_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=200,
        ),
        processed_at=at,
        type="span.model_request_end",
    )


def _spaced(n: int) -> list[BetaManagedAgentsSpanModelRequestEndEvent]:
    """`n` calls an hour apart, so the cursor overlap never spans two of them."""
    return [_request_end(f"evt_{i:02d}", at=NOW + timedelta(hours=i)) for i in range(n)]


def _paged_router(
    events: list[BetaManagedAgentsSpanModelRequestEndEvent],
    *,
    page_size: int,
    reads: list[httpx.URL] | None = None,
    stall_s: float = 0.0,
    stall_on_read: int | None = None,
) -> MARouter:
    """events.list like MA: oldest first, `created_at[gte]` filter, cursor pages.

    `stall_on_read` blocks that read (1-based) for `stall_s` -- a slow upstream.
    """
    router = MARouter()
    count = [0]

    def _list(request: httpx.Request, _match: object) -> httpx.Response:
        count[0] += 1
        if reads is not None:
            reads.append(request.url)
        if stall_on_read == count[0]:
            time.sleep(stall_s)
        since_raw = request.url.params.get("created_at[gte]")
        since = datetime.fromisoformat(since_raw) if since_raw else None
        listed = [e for e in events if since is None or e.processed_at >= since]
        start = int(request.url.params.get("page") or 0)
        end = start + page_size
        return httpx.Response(
            200,
            json={
                "data": [e.model_dump(mode="json") for e in listed[start:end]],
                "next_page": str(end) if end < len(listed) else None,
            },
        )

    router.add("GET", r"/v1/sessions/[^/]+/events", _list)
    return router


class _SlowSessions:
    """A sessionmaker whose `slow_call`-th session takes `delay_s` to open."""

    def __init__(
        self, inner: async_sessionmaker[AsyncSession], *, slow_call: int, delay_s: float
    ) -> None:
        self.inner = inner
        self.slow_call = slow_call
        self.delay_s = delay_s
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self._open(slow=self.calls == self.slow_call)

    @asynccontextmanager
    async def _open(self, *, slow: bool) -> AsyncIterator[AsyncSession]:
        if slow:
            await asyncio.sleep(self.delay_s)
        async with self.inner() as s:
            yield s


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


async def _reconcile(
    deps: TurnDeps,
    tenant_id: uuid.UUID,
    sessionmaker: Any = None,
    **bounds: Any,
) -> Any:
    return await reconcile_unbilled(
        anthropic=deps.anthropic,
        sessionmaker=cast(async_sessionmaker[AsyncSession], sessionmaker or deps.sessionmaker),
        managed_session_id=SESSION,
        record=_recorder(deps, tenant_id),
        **bounds,
    )


async def test_gap_requests_are_debited_with_the_turn_key(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    deps = _deps(db_session_factory, _paged_router(_spaced(5), page_size=2))

    with structlog.testing.capture_logs() as logs:
        result = await _reconcile(deps, tenant.id)

    ledger = await _ledger(db_session)
    assert set(ledger) == {f"turn:{SESSION}:evt_{i:02d}" for i in range(5)}, (
        "every unbilled call across all pages gets the live path's idempotency key"
    )
    assert (result.scanned, result.missing, result.debited) == (5, 5, 5)
    assert not result.truncated
    assert result.debit_usd == -sum(ledger.values()), "reported USD is what this pass inserted"
    reconciled = [e for e in logs if e["event"] == "billing.reconciled_unbilled"]
    assert len(reconciled) == 1, "one structured line per pass that finds missing calls"
    assert reconciled[0]["debited"] == 5
    assert reconciled[0]["debit_usd"] == str(result.debit_usd)
    rows = (await db_session.execute(select(TenantLedger))).scalars().all()
    assert {r.reason for r in rows} == {"turn_debit"}
    assert {r.channel_id for r in rows} == {"chan-1"}, "budget channel attribution is kept"
    async with db_session_factory() as s:
        covered = await usage_reconcile_progress.get_covered_through(s, managed_session_id=SESSION)
    assert covered == NOW + timedelta(hours=4), "the cursor records the newest call covered"


async def test_reconciling_twice_never_double_debits(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    deps = _deps(db_session_factory, _paged_router(_spaced(3), page_size=100))

    first = await _reconcile(deps, tenant.id)
    balance_after_first = sum((await _ledger(db_session)).values())
    second = await _reconcile(deps, tenant.id)

    assert first.debited == 3
    assert (second.missing, second.debited, second.debit_usd) == (0, 0, 0)
    ledger = await _ledger(db_session)
    assert len(ledger) == 3, "one ledger row per model call"
    assert sum(ledger.values()) == balance_after_first


async def test_requests_already_debited_live_are_skipped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = _spaced(4)
    deps = _deps(db_session_factory, _paged_router(events, page_size=100))
    live = _recorder(deps, tenant.id)
    # The live stream metered the first two before the worker went away.
    await live(event=events[0])
    await live(event=events[1])

    handed: list[str] = []

    async def counting(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> Decimal | None:
        handed.append(event.id)
        return await live(event=event)

    result = await reconcile_unbilled(
        anthropic=deps.anthropic,
        sessionmaker=db_session_factory,
        managed_session_id=SESSION,
        record=counting,
    )

    assert handed == ["evt_02", "evt_03"], "metered calls never reach the recorder again"
    assert (result.scanned, result.debited) == (4, 2)
    assert len(await _ledger(db_session)) == 4
    usage_rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(usage_rows) == 4


async def test_a_concurrent_winner_is_not_reported_as_reconciled(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The live stream or sweep can insert the key between the page read and the
    debit; the pass then wrote nothing and must not claim that spend."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = _spaced(2)
    deps = _deps(db_session_factory, _paged_router(events, page_size=100))
    live = _recorder(deps, tenant.id)

    async def raced(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> Decimal | None:
        if event.id == "evt_00":
            await live(event=event)  # another writer wins the key first
        return await live(event=event)

    result = await reconcile_unbilled(
        anthropic=deps.anthropic,
        sessionmaker=db_session_factory,
        managed_session_id=SESSION,
        record=raced,
    )

    ledger = await _ledger(db_session)
    assert len(ledger) == 2
    assert (result.missing, result.debited) == (2, 1)
    assert result.debit_usd == -ledger[f"turn:{SESSION}:evt_01"]


async def test_reconciled_price_matches_the_live_path(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    live_event = _request_end("evt_live", input_tokens=12_345, output_tokens=678)
    gap_event = _request_end(
        "evt_gap", input_tokens=12_345, output_tokens=678, at=NOW + timedelta(minutes=1)
    )
    deps = _deps(db_session_factory, _paged_router([live_event, gap_event], page_size=100))
    record = _recorder(deps, tenant.id)
    await record(event=live_event)

    await _reconcile(deps, tenant.id)

    ledger = await _ledger(db_session)
    live = ledger[f"turn:{SESSION}:evt_live"]
    gap = ledger[f"turn:{SESSION}:evt_gap"]
    expected = -debit_amount(cost_of(gap_event.model_usage, MODEL_PRICING[MODEL]), markup=MARKUP)
    assert gap == live, "a backfilled call costs exactly what the live stream would charge"
    assert gap == expected, "priced at the session model with the deployment markup"
    assert gap < 0


async def test_capped_passes_resume_until_every_old_gap_is_billed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    reads: list[httpx.URL] = []
    deps = _deps(db_session_factory, _paged_router(_spaced(10), page_size=3, reads=reads))

    first = await _reconcile(deps, tenant.id, page_size=3, max_pages=2)
    assert first.truncated and first.debited == 6
    assert len(reads) == 2, "never reads past the page cap"
    assert reads[0].params["order"] == "asc"
    assert reads[0].params["types[]"] == "span.model_request_end"
    assert "created_at[gte]" not in reads[0].params, "a never-read session starts at the top"

    reads.clear()
    second = await _reconcile(deps, tenant.id, page_size=3, max_pages=2)
    assert second.debited == 4 and not second.truncated
    assert reads[0].params["created_at[gte]"].startswith("2026-10-11T13:55"), (
        "resumes at the cursor (evt_05) minus the overlap, not at the start"
    )
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_{i:02d}" for i in range(10)}

    reads.clear()
    third = await _reconcile(deps, tenant.id, page_size=3, max_pages=2)
    assert (third.scanned, third.debited) == (1, 0), "a caught-up session rereads only the overlap"
    assert len(reads) == 1


async def test_a_timed_out_pass_keeps_its_progress_and_the_next_resumes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    reads: list[httpx.URL] = []
    router = _paged_router(_spaced(6), page_size=2, reads=reads, stall_s=0.3, stall_on_read=2)
    deps = _deps(db_session_factory, router)

    with structlog.testing.capture_logs() as logs:
        first = await _reconcile(deps, tenant.id, page_size=2, budget_s=0.1)
    assert first.truncated and first.debited == 2, "the first page committed before the stall"
    assert [e["truncated"] for e in logs if e["event"] == "billing.reconciled_unbilled"] == [True]

    second = await _reconcile(deps, tenant.id, page_size=2)
    assert second.debited == 4
    assert len(await _ledger(db_session)) == 6


async def test_a_slow_database_cannot_outlive_the_budget(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The budget covers the cursor read (first session) and the cursor write
    (third session), not just the MA reads."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    deps = _deps(db_session_factory, _paged_router(_spaced(1), page_size=10))

    for slow_call in (1, 3):
        slow = _SlowSessions(db_session_factory, slow_call=slow_call, delay_s=5.0)
        started = time.monotonic()
        result = await _reconcile(deps, tenant.id, sessionmaker=slow, budget_s=0.05)
        assert time.monotonic() - started < 1.0, f"session {slow_call} held the pass"
        assert result.truncated


async def test_an_upstream_error_propagates_to_the_caller(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import anthropic

    tenant = await make_tenant(db_session)
    await db_session.commit()
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda _r, _m: httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}
        ),
    )
    deps = _deps(db_session_factory, router)

    with pytest.raises(anthropic.BadRequestError):
        await _reconcile(deps, tenant.id)


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
        result = await _reconcile(deps, tenant.id)

    assert result.debited == 0 and not result.truncated
    assert not [e for e in logs if e["event"] == "billing.reconciled_unbilled"]
