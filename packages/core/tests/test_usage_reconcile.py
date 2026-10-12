"""Tests for daimon.core.usage_reconcile -- debiting model calls no live stream metered.

The prod shape (#17): a deploy restarted the worker streaming a turn; the MA
session kept making model calls server-side with no recorder attached, and the
next turn on the session only billed its own calls. `reconcile_unbilled` lists
the session's `span.model_request_end` events and debits the ones the ledger
does not hold yet, through the turn's own recorder.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
from daimon.core.mux_backend import resource_scope
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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
    """events.list like MA: `processed_at` order, `created_at[gte|lt]` filters on
    `processed_at`, cursor pages. Reads `events` when called, so a test can make a
    call listable late by inserting it.

    Stalls asynchronously for `stall_s` on read number `stall_on_read` (1-based),
    or on every read when `stall_on_read` is None -- a slow upstream.
    """
    router = MARouter()
    count = [0]

    async def _list(request: httpx.Request, _match: object) -> httpx.Response:
        count[0] += 1
        if reads is not None:
            reads.append(request.url)
        if stall_s and stall_on_read in (None, count[0]):
            await asyncio.sleep(stall_s)
        params = request.url.params
        gte = (
            datetime.fromisoformat(params["created_at[gte]"])
            if "created_at[gte]" in params
            else None
        )
        lt = (
            datetime.fromisoformat(params["created_at[lt]"]) if "created_at[lt]" in params else None
        )
        listed = sorted(
            (
                e
                for e in events
                if (gte is None or e.processed_at >= gte) and (lt is None or e.processed_at < lt)
            ),
            key=lambda e: e.processed_at,
        )
        start = int(params.get("page") or 0)
        end = start + page_size
        return httpx.Response(
            200,
            json={
                "data": [e.model_dump(mode="json") for e in listed[start:end]],
                "next_page": str(end) if end < len(listed) else None,
            },
        )

    router.add("GET", r"/v1/sessions/[^/]+/events", cast(Any, _list))
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
        scope=resource_scope(
            tenant_id=str(tenant_id), account_id="account-1", authorization_id="offline-reconcile"
        ),
        record=_recorder(deps, tenant_id),
        **bounds,
    )


# A slow upstream for the timeout regressions: one read takes SLOW_READ_S and a
# pass's budget fits one read plus its database work, but never two reads. The
# margin keeps them deterministic on a loaded CI runner.
SLOW_READ_S = 0.5
ONE_READ_BUDGET_S = 0.8


async def _passes_until_billed(
    deps: TurnDeps,
    tenant_id: uuid.UUID,
    db_session: AsyncSession,
    *,
    want: int,
    max_passes: int,
    **bounds: Any,
) -> list[Any]:
    """Run passes until `want` calls are debited (or `max_passes` ran). A stalled
    cursor never gets there; a slow but progressing one does."""
    results: list[Any] = []
    while len(results) < max_passes and len(await _ledger(db_session)) < want:
        results.append(await _reconcile(deps, tenant_id, **bounds))
    return results


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
        progress = await usage_reconcile_progress.get(s, managed_session_id=SESSION)
    assert progress is not None
    assert progress.covered_through == NOW + timedelta(hours=4), "cursor at the newest call"
    assert progress.covered_ids == {"evt_04"}, "with the ids handled at exactly that time"


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
        scope=resource_scope(
            tenant_id=str(tenant.id), account_id="account-1", authorization_id="offline-reconcile"
        ),
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
        scope=resource_scope(
            tenant_id=str(tenant.id), account_id="account-1", authorization_id="offline-reconcile"
        ),
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
    forward = [r for r in reads if "created_at[lt]" not in r.params]
    assert len(forward) == 2, "never reads past the page cap"
    assert forward[0].params["order"] == "asc"
    assert forward[0].params["types[]"] == "span.model_request_end"
    assert "created_at[gte]" not in forward[0].params, "a never-read session starts at the top"

    reads.clear()
    second = await _reconcile(deps, tenant.id, page_size=3, max_pages=2)
    assert second.debited == 4 and not second.truncated
    forward = [r for r in reads if "created_at[lt]" not in r.params]
    assert forward[0].params["page"] == "6", "continues the unfinished listing with MA's cursor"
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_{i:02d}" for i in range(10)}

    reads.clear()
    third = await _reconcile(deps, tenant.id, page_size=3, max_pages=2)
    assert third.debited == 0 and not third.truncated
    forward = [r for r in reads if "created_at[lt]" not in r.params]
    assert [r.params["created_at[gte]"][:19] for r in forward] == ["2026-10-11T18:00:00"], (
        "a finished listing re-opens at the newest call handled"
    )
    assert len(reads) == 2, "a caught-up session costs one forward read and one repair read"


@pytest.mark.parametrize("step_s", [0, 20], ids=["equal-timestamps", "dense"])
async def test_dense_or_tied_calls_never_stall_a_capped_scan(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    step_s: int,
) -> None:
    """Review regression: calls closer together than any overlap, or sharing one
    timestamp, are still all billed by successive capped passes."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [
        _request_end(f"evt_{i:02d}", at=NOW + timedelta(seconds=i * step_s)) for i in range(10)
    ]
    deps = _deps(db_session_factory, _paged_router(events, page_size=3))

    debited = [
        (await _reconcile(deps, tenant.id, page_size=3, max_pages=2)).debited for _ in range(4)
    ]

    assert len(await _ledger(db_session)) == 10, f"debited per pass: {debited}"
    assert debited[:2] == [6, 4]


async def test_repeated_timeouts_still_make_progress(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review regression: every read is slow and each pass's budget fits about one
    page; each pass still commits and resumes past what the last one billed."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end(f"evt_{i:02d}", at=NOW + timedelta(seconds=i * 20)) for i in range(6)]
    deps = _deps(db_session_factory, _paged_router(events, page_size=3, stall_s=SLOW_READ_S))

    results = await _passes_until_billed(
        deps, tenant.id, db_session, want=6, max_passes=6, page_size=3, budget_s=ONE_READ_BUDGET_S
    )

    assert len(await _ledger(db_session)) == 6, f"debited per pass: {[r.debited for r in results]}"


async def test_a_call_listed_late_is_found_by_the_repair_scan(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review regression: a call ten minutes behind the forward cursor that MA only
    lists after newer ones is billed by the repair rescan."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end("evt_new", at=NOW + timedelta(minutes=10))]
    deps = _deps(db_session_factory, _paged_router(events, page_size=100))
    await _reconcile(deps, tenant.id)
    events.insert(0, _request_end("evt_late", at=NOW))

    result = await _reconcile(deps, tenant.id)

    assert (result.repaired, result.debited) == (1, 1)
    assert f"turn:{SESSION}:evt_late" in await _ledger(db_session)


async def test_the_repair_scan_cycles_through_history_one_page_a_pass(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    reads: list[httpx.URL] = []
    deps = _deps(db_session_factory, _paged_router(_spaced(7), page_size=3, reads=reads))

    await _reconcile(deps, tenant.id, page_size=3)  # bills everything; cursor at evt_06
    repair_pages: list[str | None] = []
    for _ in range(4):
        reads.clear()
        await _reconcile(deps, tenant.id, page_size=3)
        repair = [r for r in reads if "created_at[lt]" in r.params]
        assert len(repair) == 1, "exactly one repair read per pass"
        repair_pages.append(repair[0].params.get("page"))

    assert repair_pages == [None, "3", None, "3"], (
        "each cycle starts at the top, continues with MA's cursor, and restarts at the end"
    )


async def test_a_timed_out_pass_keeps_its_progress_and_the_next_resumes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    router = _paged_router(_spaced(6), page_size=2, stall_s=5.0, stall_on_read=2)
    deps = _deps(db_session_factory, router)

    with structlog.testing.capture_logs() as logs:
        first = await _reconcile(deps, tenant.id, page_size=2, budget_s=1.0)
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


async def test_concurrent_cursor_writes_never_move_it_backwards(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
) -> None:
    """Separate connections, as two workers' passes would use."""
    await db_session.close()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        await usage_reconcile_progress.begin_pass(s, managed_session_id=SESSION)

    async def advance(at: datetime, ids: list[str]) -> None:
        async with factory() as s, s.begin():
            await usage_reconcile_progress.advance(
                s, managed_session_id=SESSION, through=at, ids=ids, since=None, page=None
            )

    async def mark() -> tuple[datetime | None, frozenset[str]]:
        async with factory() as s:
            progress = await usage_reconcile_progress.get(s, managed_session_id=SESSION)
        assert progress is not None
        return progress.covered_through, progress.covered_ids

    await asyncio.gather(advance(NOW, ["a"]), advance(NOW, ["b"]))
    assert await mark() == (NOW, {"a", "b"}), "the same time merges the boundary ids"
    later = NOW + timedelta(minutes=1)
    await asyncio.gather(advance(later, ["c"]), advance(NOW, ["d"]))
    assert await mark() == (later, {"c"}), "a later time wins; an older write is ignored"


async def test_tied_timestamps_with_repeated_timeouts_eventually_finish(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review regression (B3a): nine calls share one timestamp, every read is slow
    and a pass's budget fits one page; each pass reads the next unread page."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end(f"evt_tied_{i:02d}") for i in range(9)]
    deps = _deps(db_session_factory, _paged_router(events, page_size=3, stall_s=SLOW_READ_S))

    results = await _passes_until_billed(
        deps, tenant.id, db_session, want=9, max_passes=6, page_size=3, budget_s=ONE_READ_BUDGET_S
    )

    assert len(await _ledger(db_session)) == 9, f"debited per pass: {[r.debited for r in results]}"
    assert all(r.debited for r in results), "every pass bills new calls; none rereads old pages"


async def test_tied_repair_listing_survives_repeated_timeouts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review regression (B3a): nine late calls share one timestamp behind a newer
    covered call, and only repair reads are slow."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end("evt_new", at=NOW + timedelta(minutes=10))]
    deps = _deps(db_session_factory, _paged_router(events, page_size=3))
    await _reconcile(deps, tenant.id)
    events[:0] = [_request_end(f"evt_late_{i:02d}") for i in range(9)]
    router = _paged_router(events, page_size=3)

    async def slow_repair(request: httpx.Request) -> httpx.Response:
        if "created_at[lt]" in request.url.params:
            await asyncio.sleep(SLOW_READ_S)
        return await cast(Any, router.dispatch(request))

    deps = dataclasses.replace(deps, anthropic=build_fake_anthropic(cast(Any, slow_repair)))

    results = await _passes_until_billed(
        deps, tenant.id, db_session, want=10, max_passes=6, page_size=3, budget_s=ONE_READ_BUDGET_S
    )

    assert len(await _ledger(db_session)) == 10, f"debited per pass: {[r.debited for r in results]}"
    assert all(r.debited for r in results), "every pass bills new calls; none rereads old pages"


async def test_repair_gets_its_turn_when_every_read_is_slow(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review regression (B3b): a caught-up session whose forward read takes most
    of the budget still runs its repair scan on alternate passes, and a pass that
    misses a due repair page says so."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end("evt_new", at=NOW + timedelta(minutes=10))]
    await _reconcile(_deps(db_session_factory, _paged_router(events, page_size=3)), tenant.id)
    events.insert(0, _request_end("evt_late", at=NOW))
    deps = _deps(db_session_factory, _paged_router(events, page_size=3, stall_s=SLOW_READ_S))

    with structlog.testing.capture_logs() as logs:
        results = await _passes_until_billed(
            deps,
            tenant.id,
            db_session,
            want=2,
            max_passes=3,
            page_size=3,
            budget_s=ONE_READ_BUDGET_S,
        )

    assert f"turn:{SESSION}:evt_late" in await _ledger(db_session)
    assert sum(r.repaired for r in results) == 1
    assert any(r.repair_skipped for r in results), "a pass that missed its repair page reports it"
    skipped = [
        e for e in logs if e["event"] == "billing.reconciled_unbilled" and e["repair_skipped"]
    ]
    assert skipped, "and logs it"


async def test_a_new_call_at_the_boundary_time_is_billed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    events = [_request_end("evt_boundary_a")]
    deps = _deps(db_session_factory, _paged_router(events, page_size=3))
    await _reconcile(deps, tenant.id)
    events.insert(0, _request_end("evt_boundary_b"))

    result = await _reconcile(deps, tenant.id)

    assert result.debited == 1 and len(await _ledger(db_session)) == 2
    async with db_session_factory() as s:
        progress = await usage_reconcile_progress.get(s, managed_session_id=SESSION)
    assert progress is not None
    assert progress.covered_ids == {"evt_boundary_a", "evt_boundary_b"}
