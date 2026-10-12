"""A failed routine run tells its destination once per failing streak (QA #3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.errors import TurnError
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    failure_notice_text,
    poll_deliveries_once,
    record_failure,
    render_fallback_dm,
    render_fallback_post,
)
from daimon.core.scheduler import run_one_tick
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    FAILURE_NOTICE,
    create_routine,
    get_routine,
    record_result,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_PDF_ERROR = "TurnError: upstream: The PDF specified was not valid"
_MODEL_NOTICE = (
    "The request to the AI model failed.\n\nIt will try again at its next scheduled time."
)


@pytest.fixture
def db_session_factory(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def _routine(
    session: AsyncSession,
    *,
    destination: bool = True,
    next_fire_at: datetime | None = None,
) -> RoutineRow:
    tenant = await make_tenant(session)
    row = await create_routine(
        session,
        tenant_id=tenant.id,
        created_by_user_id="u1",
        agent_id="agent_a",
        agent_name="team-020-analyst",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="summarize",
        next_fire_at=next_fire_at,
        destination_kind="channel" if destination else None,
        destination_id="111222333" if destination else None,
    )
    await session.commit()
    return row


async def _fail(sm: async_sessionmaker[AsyncSession], row: RoutineRow, error: str) -> RoutineRow:
    async with sm() as s, s.begin():
        await record_failure(s, row.id, error=error)
    return await _get(sm, row)


async def _get(sm: async_sessionmaker[AsyncSession], row: RoutineRow) -> RoutineRow:
    async with sm() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None
    return fetched


async def _deliver(sm: async_sessionmaker[AsyncSession]) -> list[str]:
    posted: list[str] = []

    async def post(claimed: RoutineRow) -> DeliveryOutcome:
        posted.append(render_fallback_post(claimed))
        return DeliveryOutcome(status="delivered")

    await poll_deliveries_once(sm, platform="discord", post=post, now=datetime.now(UTC))
    return posted


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_PDF_ERROR, _MODEL_NOTICE),
        ("BadRequestError: Error code: 400 - secret detail", _MODEL_NOTICE),
        (
            "timeout: exceeded 3000.0s",
            "It took too long, so Daimon stopped it.\n\n"
            "It will try again at its next scheduled time.",
        ),
        (
            "TurnError: ceiling: turn exceeded 45 minutes",
            "It took too long, so Daimon stopped it.\n\n"
            "It will try again at its next scheduled time.",
        ),
        (
            "balance_depleted",
            "Your team's Daimon credit has run out.\n\n"
            "It will run again at its next scheduled time once an admin adds credit.",
        ),
        (
            "KeyError: 'x'",
            "Something went wrong while it was running.\n\n"
            "It will try again at its next scheduled time.",
        ),
    ],
)
def test_notice_says_what_failed_in_plain_words(error: str, expected: str) -> None:
    assert failure_notice_text(error) == expected
    assert "secret detail" not in expected and "PDF" not in expected, "no raw error text"


@pytest.mark.parametrize(
    "error",
    ["scheduler_shutdown", "invoker_not_allowed", "access_policy_unreadable", "own_agents_only"],
)
def test_shutdowns_and_policy_refusals_stay_quiet(error: str) -> None:
    assert failure_notice_text(error) is None


async def test_failure_notice_reads_as_a_failure_not_a_result(db_session: AsyncSession) -> None:
    saved = await _routine(db_session)
    row = saved.model_copy(
        update={"delivery_note": FAILURE_NOTICE, "delivery_payload": _MODEL_NOTICE}
    )
    assert render_fallback_post(row) == (
        "team-020-analyst's daily routine at 09:00 UTC failed, so there's no result this time."
        f"\n\n{_MODEL_NOTICE}"
    )
    assert render_fallback_post(row, as_agent=True).startswith(
        "The daily routine at 09:00 UTC failed, so there's no result this time."
    )
    dm = render_fallback_dm(row, "destination_unavailable")
    assert "Here's the result" not in dm
    assert dm.endswith(f"Your routine failed, so there's no result this time.\n\n{_MODEL_NOTICE}")


async def test_a_failed_run_posts_one_notice_per_failing_streak(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    row = await _routine(db_session)

    failed = await _fail(sm, row, _PDF_ERROR)
    assert (failed.delivery_status, failed.delivery_note) == ("pending", FAILURE_NOTICE)
    assert failed.delivery_payload == _MODEL_NOTICE
    assert failed.last_error == _PDF_ERROR, "the panel still sees the raw error"

    posted = await _deliver(sm)
    assert posted == [
        "team-020-analyst's daily routine at 09:00 UTC failed, so there's no result this time."
        f"\n\n{_MODEL_NOTICE}"
    ]

    again = await _fail(sm, row, _PDF_ERROR)
    assert again.delivery_status == "delivered", "a repeat failure queues nothing"
    assert await _deliver(sm) == []

    async with sm() as s, s.begin():
        await record_result(s, row.id, tail="All green.", error=None, delivery="pending")
    assert len(await _deliver(sm)) == 1, "the success posts its result"

    after_recovery = await _fail(sm, row, "timeout: exceeded 3000.0s")
    assert (after_recovery.delivery_status, after_recovery.delivery_note) == (
        "pending",
        FAILURE_NOTICE,
    ), "failing again after a success is a new streak"


async def test_a_pending_result_is_not_replaced_by_a_notice(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    row = await _routine(db_session)
    async with sm() as s, s.begin():
        await record_result(s, row.id, tail="All green.", error=None, delivery="pending")

    failed = await _fail(sm, row, _PDF_ERROR)

    assert (failed.delivery_status, failed.delivery_payload) == ("pending", "All green.")
    assert failed.delivery_note is None


async def test_no_notice_without_a_destination_or_for_a_quiet_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    headless = await _routine(db_session, destination=False)
    assert (await _fail(sm, headless, _PDF_ERROR)).delivery_status is None

    deployed = await _routine(db_session)
    assert (await _fail(sm, deployed, "scheduler_shutdown")).delivery_status is None


async def test_a_fire_that_raises_queues_the_notice_through_the_tick(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    row = await _routine(db_session, next_fire_at=now - timedelta(minutes=1))

    async def failing_fire(_row: RoutineRow) -> None:
        raise TurnError(kind="upstream", message="The PDF specified was not valid")

    class _NoCap:
        async def is_over_cap(self, tenant_id: object, user_id: str) -> bool:
            return False

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        caps=_NoCap(),
        fire=failing_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=1,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    after = await _get(db_session_factory, row)
    assert after.last_error == _PDF_ERROR
    assert (after.delivery_status, after.delivery_payload) == ("pending", _MODEL_NOTICE)
