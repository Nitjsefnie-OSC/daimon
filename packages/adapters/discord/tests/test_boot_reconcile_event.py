"""The first on_ready logs one aggregate event once its boot reconcile is done.

The staging deploy waits on `discord.boot_reconcile_complete` (or
`discord.boot_reconcile_failed`) before running `daimon defaults verify`, so
the event must come after the slowest tenant's reconcile, cover every failure
path and fire once per process. These tests stub the DB calls on_ready makes,
so they run without Postgres.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import structlog.testing
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.testing.turn_fakes import deps_for
from structlog.typing import EventDict

from .harness import make_bot

_BOT = "daimon.adapters.discord.bot"
_EVENTS = ("discord.boot_reconcile_complete", "discord.boot_reconcile_failed")

Reconcile = Callable[..., Awaitable[ApplyReport]]


def _runtime() -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.defaults_root = MagicMock()
    settings.discord = None
    return DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(agent_name=None),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None),
    )


def _guild(guild_id: int) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "Boot Guild"
    guild.unavailable = False
    guild.me = MagicMock(spec=discord.Member)
    guild.me.guild_permissions = MagicMock()
    return guild


def _tenant(guild_id: int) -> SimpleNamespace:
    """A ready, unarchived tenant row: on_ready reconciles it without posting."""
    return SimpleNamespace(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, str(guild_id)),
        external_id=str(guild_id),
        provision_status="ready",
        archived_at=None,
    )


def _failing_report() -> ApplyReport:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="agent", name="a", action=Action.FAILED))
    return report


def _bot(guild_ids: list[int]) -> DaimonBot:
    bot = make_bot(_runtime())
    bot._connection._guilds = {gid: _guild(gid) for gid in guild_ids}  # pyright: ignore[reportPrivateUsage]
    bot._retire_orphaned_turns = AsyncMock()  # type: ignore[method-assign]
    bot.tree.clear_commands = MagicMock()  # type: ignore[method-assign]
    bot.tree.sync = AsyncMock()  # type: ignore[method-assign]
    return bot


async def _drain(bot: DaimonBot) -> None:
    while bot._bg_tasks:  # pyright: ignore[reportPrivateUsage]
        await asyncio.gather(*list(bot._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]


def _boot_events(logs: list[EventDict]) -> list[EventDict]:
    return [entry for entry in logs if entry["event"] in _EVENTS]


async def _run_ready(
    bot: DaimonBot, tenants: list[SimpleNamespace], reconcile: Reconcile, *, times: int = 1
) -> list[EventDict]:
    with (
        patch(f"{_BOT}.list_tenants_by_platform", new_callable=AsyncMock, return_value=tenants),
        patch(f"{_BOT}.reconcile_tenant_defaults", side_effect=reconcile),
        patch(f"{_BOT}.set_provision_status", new_callable=AsyncMock),
        structlog.testing.capture_logs() as logs,
    ):
        for _ in range(times):
            await bot.on_ready()
            await _drain(bot)
    return logs


async def _ok(*_args: object, **_kwargs: object) -> ApplyReport:
    return ApplyReport()


async def test_complete_after_every_tenant_reconciles() -> None:
    ids = [910000001, 910000002]
    bot = _bot(ids)

    logs = await _run_ready(bot, [_tenant(gid) for gid in ids], _ok)

    events = _boot_events(logs)
    assert len(events) == 1
    assert events[0]["event"] == "discord.boot_reconcile_complete"
    assert events[0]["log_level"] == "info"
    assert (events[0]["tenants"], events[0]["succeeded"], events[0]["failed"]) == (2, 2, 0)


@pytest.mark.parametrize(
    ("outcome", "per_tenant_event"),
    [
        (DaimonError("provider down"), "guild_seed_failed"),
        (RuntimeError("bug"), "guild_seed_unexpected"),
        (_failing_report(), "guild_reconcile_failed_ready_tenant"),
    ],
    ids=["seed_failed", "seed_unexpected", "failing_report"],
)
async def test_failed_when_one_tenant_seed_fails(
    outcome: BaseException | ApplyReport, per_tenant_event: str
) -> None:
    ok_id, bad_id = 910000011, 910000012
    bad_tenant = _tenant(bad_id)
    bot = _bot([ok_id, bad_id])

    async def reconcile(*_args: object, tenant_id: uuid.UUID, **_kwargs: object) -> ApplyReport:
        if tenant_id != bad_tenant.id:
            return ApplyReport()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    logs = await _run_ready(bot, [_tenant(ok_id), bad_tenant], reconcile)

    assert per_tenant_event in [entry["event"] for entry in logs], "per-tenant logging stays"
    events = _boot_events(logs)
    assert len(events) == 1
    assert events[0]["event"] == "discord.boot_reconcile_failed"
    assert events[0]["log_level"] == "warning"
    assert (events[0]["tenants"], events[0]["succeeded"], events[0]["failed"]) == (2, 1, 1)
    assert events[0]["aborted"] is False


async def test_failed_when_an_unjoined_tenant_reconcile_fails() -> None:
    absent = _tenant(910000021)  # registered, but its guild is not in the cache
    bot = _bot([])

    async def reconcile(*_args: object, **_kwargs: object) -> ApplyReport:
        raise RuntimeError("provider down")

    logs = await _run_ready(bot, [absent], reconcile)

    events = _boot_events(logs)
    assert [entry["event"] for entry in events] == ["discord.boot_reconcile_failed"]
    assert events[0]["failed"] == 1


@pytest.mark.parametrize("escape", [RuntimeError("escaped"), asyncio.CancelledError()])
async def test_failed_when_a_reconcile_task_raises_or_is_cancelled(
    escape: BaseException,
) -> None:
    ok_id, bad_id = 910000031, 910000032
    bad_tenant = _tenant(bad_id)
    bot = _bot([ok_id, bad_id])
    original_seed = bot._seed_tenant_defaults  # pyright: ignore[reportPrivateUsage]

    async def seed(*, tenant_id: uuid.UUID, guild: discord.Guild, was_ready: bool) -> bool | None:
        if tenant_id == bad_tenant.id:
            raise escape
        return await original_seed(tenant_id=tenant_id, guild=guild, was_ready=was_ready)

    bot._seed_tenant_defaults = seed  # type: ignore[method-assign]

    logs = await _run_ready(bot, [_tenant(ok_id), bad_tenant], _ok)

    events = _boot_events(logs)
    assert [entry["event"] for entry in events] == ["discord.boot_reconcile_failed"]
    assert (events[0]["succeeded"], events[0]["failed"]) == (1, 1)


async def test_zero_tenants_still_completes_promptly() -> None:
    bot = _bot([])

    with (
        patch(f"{_BOT}.list_tenants_by_platform", new_callable=AsyncMock, return_value=[]),
        structlog.testing.capture_logs() as logs,
    ):
        await bot.on_ready()
        # The watcher has nothing to await: one scheduler pass is enough.
        await asyncio.sleep(0)

    events = _boot_events(logs)
    assert [entry["event"] for entry in events] == ["discord.boot_reconcile_complete"]
    assert events[0]["tenants"] == 0


async def test_a_second_on_ready_emits_nothing_new() -> None:
    """A gateway reconnect re-fires on_ready, which still re-runs a reconcile that
    failed the first time. The boot event belongs to the first on_ready only."""
    gid = 910000041
    tenant = _tenant(gid)
    bot = _bot([gid])
    calls: list[uuid.UUID] = []

    async def reconcile(*_args: object, tenant_id: uuid.UUID, **_kwargs: object) -> ApplyReport:
        calls.append(tenant_id)
        return _failing_report() if len(calls) == 1 else ApplyReport()

    logs = await _run_ready(bot, [tenant], reconcile, times=2)

    assert calls == [tenant.id, tenant.id], "the reconnect still re-runs the failed reconcile"
    events = _boot_events(logs)
    assert [entry["event"] for entry in events] == ["discord.boot_reconcile_failed"]


async def test_the_event_waits_for_the_slowest_tenant() -> None:
    fast_id, slow_id = 910000051, 910000052
    slow_tenant = _tenant(slow_id)
    bot = _bot([fast_id, slow_id])
    release_slow = asyncio.Event()
    finished: list[uuid.UUID] = []

    async def reconcile(*_args: object, tenant_id: uuid.UUID, **_kwargs: object) -> ApplyReport:
        if tenant_id == slow_tenant.id:
            await release_slow.wait()
        finished.append(tenant_id)
        return ApplyReport()

    with (
        patch(
            f"{_BOT}.list_tenants_by_platform",
            new_callable=AsyncMock,
            return_value=[_tenant(fast_id), slow_tenant],
        ),
        patch(f"{_BOT}.reconcile_tenant_defaults", side_effect=reconcile),
        patch(f"{_BOT}.set_provision_status", new_callable=AsyncMock),
        structlog.testing.capture_logs() as logs,
    ):
        await bot.on_ready()
        for _ in range(50):
            await asyncio.sleep(0)
        assert finished == [_tenant(fast_id).id], "the fast tenant is done, the slow one is not"
        assert _boot_events(logs) == [], "no boot event while a reconcile is still running"
        release_slow.set()
        await _drain(bot)

    assert finished[-1] == slow_tenant.id
    events = _boot_events(logs)
    assert [entry["event"] for entry in events] == ["discord.boot_reconcile_complete"]
    assert events[0]["tenants"] == 2


async def test_an_aborted_first_on_ready_reports_failed_once() -> None:
    """on_ready raising part-way through the sweep is a failed boot reconcile, and
    the later on_ready that finishes the sweep does not log a second event."""
    known_id, new_id = 910000061, 910000062
    bot = _bot([known_id, new_id])
    provision = AsyncMock(side_effect=[RuntimeError("db down"), None])

    with (
        patch.object(bot, "_provision_joined_guild", provision),
        patch(
            f"{_BOT}.list_tenants_by_platform",
            new_callable=AsyncMock,
            return_value=[_tenant(known_id)],
        ),
        patch(f"{_BOT}.reconcile_tenant_defaults", side_effect=_ok),
        patch(f"{_BOT}.set_provision_status", new_callable=AsyncMock),
        structlog.testing.capture_logs() as logs,
    ):
        with pytest.raises(RuntimeError, match="db down"):
            await bot.on_ready()
        await _drain(bot)
        await bot.on_ready()
        await _drain(bot)

    events = _boot_events(logs)
    assert [entry["event"] for entry in events] == ["discord.boot_reconcile_failed"]
    assert events[0]["aborted"] is True


async def test_cancelling_the_watcher_leaves_reconcile_tasks_running() -> None:
    gid = 910000071
    bot = _bot([gid])
    release = asyncio.Event()
    done = asyncio.Event()

    async def reconcile(*_args: object, **_kwargs: object) -> ApplyReport:
        await release.wait()
        done.set()
        return ApplyReport()

    with (
        patch(
            f"{_BOT}.list_tenants_by_platform", new_callable=AsyncMock, return_value=[_tenant(gid)]
        ),
        patch(f"{_BOT}.reconcile_tenant_defaults", side_effect=reconcile),
        patch(f"{_BOT}.set_provision_status", new_callable=AsyncMock),
        structlog.testing.capture_logs() as logs,
    ):
        await bot.on_ready()
        watchers = [
            task
            for task in bot._bg_tasks  # pyright: ignore[reportPrivateUsage]
            if task.get_coro().__qualname__.endswith("_watch_boot_reconcile")  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]
        ]
        assert len(watchers) == 1, "the watcher is retained in the tracked task set"
        # Let the watcher start awaiting the reconcile before it is cancelled.
        for _ in range(10):
            await asyncio.sleep(0)
        assert not done.is_set()
        watchers[0].cancel()
        await asyncio.sleep(0)
        release.set()
        await _drain(bot)

    assert done.is_set(), "the reconcile ran to completion after the watcher was cancelled"
    assert _boot_events(logs) == []
