"""Deployment admins on Teams: the admin team's owners, plus the deprecated list."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from dataclasses import MISSING, fields
from typing import Any
from unittest.mock import MagicMock

import structlog
from daimon.adapters.teams.budget_notice import with_budget_notifier
from daimon.adapters.teams.deployment_admins import (
    admin_team_owners,
    boot_admin_ids,
    demoting_owner_reads,
    deployment_admin_ids,
    is_deployment_admin,
    live_admin_ids,
)
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.channel_admins import GroupLookupFailed, GroupMembersCache
from daimon.core.config import TeamsSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.turn.deps import TurnDeps
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AAD_OBJECT_ID, ENTRA_TENANT_ID, OTHER_AAD_OBJECT_ID, teams_settings

TEAM = "0f2c8a51-7d3e-4b9a-8c61-2e5f4a9b7c10"
FOREIGN_TENANT = "99999999-9999-9999-9999-999999999999"
REMOVED_OWNER = str(uuid.UUID(int=4))


def _settings(*, team: str | None = TEAM, admins: tuple[str, ...] = ()) -> TeamsSettings:
    return teams_settings(admins=admins).model_copy(update={"admin_team_id": team})


class _Owners:
    """Graph's owner read: the current owners, or a failure; counts its reads."""

    def __init__(self, *owners: str) -> None:
        self.owners = frozenset(owners)
        self.failure: str | None = None
        self.reads = 0

    async def __call__(self, group_id: str) -> frozenset[str]:
        assert group_id == TEAM, "only the admin team is read"
        self.reads += 1
        if self.failure is not None:
            raise GroupLookupFailed(self.failure)
        return self.owners


def _runtime(owners: _Owners, *, clock: list[float] | None = None) -> MagicMock:
    now = clock if clock is not None else [0.0]
    return MagicMock(team_owners=owners, group_members=GroupMembersCache(clock=lambda: now[0]))


async def _is_admin(
    runtime: MagicMock, settings: TeamsSettings, user_id: str, **kwargs: Any
) -> bool:
    return await is_deployment_admin(
        settings, admin_team_owners(runtime), user_id=user_id, is_external=False, **kwargs
    )


async def test_an_owner_of_the_admin_team_is_an_admin_and_a_member_is_not() -> None:
    runtime = _runtime(_Owners(AAD_OBJECT_ID))
    assert await _is_admin(runtime, _settings(), AAD_OBJECT_ID.upper()), "an owner administers"
    assert not await _is_admin(runtime, _settings(), OTHER_AAD_OBJECT_ID), "a non-owner does not"


async def test_an_owner_held_as_from_outside_is_never_an_admin() -> None:
    """A guest held as one, an external participant or a group chat with outsiders."""
    owners = admin_team_owners(_runtime(_Owners(AAD_OBJECT_ID)))
    settings = _settings(admins=(AAD_OBJECT_ID,))
    external = await is_deployment_admin(settings, owners, user_id=AAD_OBJECT_ID, is_external=True)
    assert not external, "someone answered as from outside is never an admin, listed or owner"
    foreign = await is_deployment_admin(
        settings, owners, user_id=AAD_OBJECT_ID, is_external=False, home_tenant_id=FOREIGN_TENANT
    )
    assert not foreign, "someone from another tenant is never an admin"
    home = await is_deployment_admin(
        settings, owners, user_id=AAD_OBJECT_ID, is_external=False, home_tenant_id=ENTRA_TENANT_ID
    )
    assert home, "our own tenant's owner still is"


async def test_a_failed_owner_read_grants_nothing_and_logs_once() -> None:
    owners = _Owners(AAD_OBJECT_ID)
    owners.failure = "http error (status 403)"
    runtime = _runtime(owners)
    settings = _settings(admins=(OTHER_AAD_OBJECT_ID,))
    with structlog.testing.capture_logs() as logs:
        assert not await _is_admin(runtime, settings, AAD_OBJECT_ID), "a failed read fails closed"
        assert not await _is_admin(runtime, settings, AAD_OBJECT_ID), "a cached failure too"
        assert await _is_admin(runtime, settings, OTHER_AAD_OBJECT_ID), "the old list still works"
    failed = [log for log in logs if log["event"] == "teams.admin_team.lookup_failed"]
    assert len(failed) == 1, f"one log per failed read, not per check: {logs}"
    assert failed[0]["reason"] == "http error (status 403)", "reason and status only"
    assert owners.reads == 1, "the failure is cached, not read again by every check"


async def test_the_deprecated_list_still_counts_and_warns_at_boot() -> None:
    runtime = _runtime(_Owners(AAD_OBJECT_ID))
    settings = _settings(admins=(OTHER_AAD_OBJECT_ID,))
    assert await _is_admin(runtime, settings, OTHER_AAD_OBJECT_ID), "a listed id administers"
    with structlog.testing.capture_logs() as logs:
        boot = await boot_admin_ids(runtime, settings)
    assert boot == tuple(sorted({AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID})), "owners and the list"
    warned = [log for log in logs if log["event"] == "teams.admin_user_ids_deprecated"]
    assert len(warned) == 1 and warned[0]["log_level"] == "warning", logs
    with structlog.testing.capture_logs() as quiet:
        await boot_admin_ids(runtime, _settings())
    assert not quiet, "no warning without the deprecated list"


async def test_with_neither_set_nobody_is_an_admin() -> None:
    owners = _Owners(AAD_OBJECT_ID)
    runtime = _runtime(owners)
    settings = _settings(team=None)
    assert not await _is_admin(runtime, settings, AAD_OBJECT_ID), "no team, no list: no admin"
    assert await deployment_admin_ids(settings, admin_team_owners(runtime)) == ()
    assert owners.reads == 0, "no admin team is read"


async def test_a_removed_owner_loses_admin_when_the_cache_expires() -> None:
    owners, clock = _Owners(AAD_OBJECT_ID), [0.0]
    runtime = _runtime(owners, clock=clock)
    assert await _is_admin(runtime, _settings(), AAD_OBJECT_ID)
    owners.owners = frozenset()
    assert await _is_admin(runtime, _settings(), AAD_OBJECT_ID), "cached for a minute"
    clock[0] = 61.0
    assert not await _is_admin(runtime, _settings(), AAD_OBJECT_ID), "lapsed once it expires"


async def test_notices_to_the_admins_reach_the_owners_and_the_list() -> None:
    runtime = _runtime(_Owners(AAD_OBJECT_ID))
    runtime.settings.teams = _settings(admins=(OTHER_AAD_OBJECT_ID,))
    expected = tuple(sorted({AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID}))
    assert await live_admin_ids(runtime)() == expected, "owners plus the deprecated list"
    runtime.settings.teams = _settings(team=None)
    assert await live_admin_ids(runtime)() == (), "nobody to tell, as before"


def _required(cls: Any, **given: object) -> dict[str, object]:
    needed = [f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING]
    return {name: MagicMock() for name in needed} | given


async def test_the_budget_notice_reads_the_live_admins() -> None:
    settings = MagicMock()
    settings.teams = _settings()
    runtime = TeamsRuntime(
        **_required(
            TeamsRuntime,
            settings=settings,
            turn_deps=TurnDeps(**_required(TurnDeps)),
            team_owners=_Owners(AAD_OBJECT_ID),
            group_members=GroupMembersCache(),
        )
    )
    server_admins = with_budget_notifier(runtime, MagicMock()).turn_deps.server_admins
    assert server_admins is not None, "a notifier comes with the live admins"
    assert tuple(await server_admins()) == (AAD_OBJECT_ID,), "the admin team's owners"


class _Background:
    """`spawn`: runs each demotion as a task the test can wait for."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []

    def __call__(self, coro: Coroutine[Any, Any, None]) -> None:
        self.tasks.append(asyncio.create_task(coro))


async def _stored_admins(
    db_factory: async_sessionmaker[AsyncSession], *user_ids: str
) -> dict[str, uuid.UUID]:
    tenant = await provision_tenant(db_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    accounts: dict[str, uuid.UUID] = {}
    async with db_factory.begin() as session:
        for user_id in user_ids:
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant.tenant_id, platform="teams", external_id=user_id
            )
            await set_role(session, principal.account_id, Role.ADMIN)
            accounts[user_id] = principal.account_id
    return accounts


async def _roles(
    db_factory: async_sessionmaker[AsyncSession], accounts: dict[str, uuid.UUID]
) -> dict[str, Role | None]:
    async with db_factory() as session:
        return {
            user: getattr(await get_account(session, account), "role", None)
            for user, account in accounts.items()
        }


async def test_each_owner_read_demotes_a_removed_owners_stored_role_in_the_background(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Routines and tools read the stored role, so it follows the team within the minute."""
    accounts = await _stored_admins(
        db_session_factory, AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID, REMOVED_OWNER
    )
    graph, clock, background = _Owners(AAD_OBJECT_ID), [0.0], _Background()
    runtime = _runtime(graph, clock=clock)
    runtime.settings.teams = settings = _settings(admins=(OTHER_AAD_OBJECT_ID,))
    runtime.sessionmaker = db_session_factory
    runtime.team_owners = demoting_owner_reads(runtime, graph, in_background=background)

    assert await _is_admin(runtime, settings, AAD_OBJECT_ID), "an owner administers"
    assert await _is_admin(runtime, settings, AAD_OBJECT_ID), "a cached read, no new demotion"
    assert len(background.tasks) == 1, "one demotion per read, off the caller's path"
    await asyncio.gather(*background.tasks)
    assert await _roles(db_session_factory, accounts) == {
        AAD_OBJECT_ID: Role.ADMIN,
        OTHER_AAD_OBJECT_ID: Role.ADMIN,
        REMOVED_OWNER: Role.USER,
    }, "only someone neither an owner nor listed is demoted"

    graph.failure, clock[0] = "http error (status 503)", 61.0
    await _is_admin(runtime, settings, AAD_OBJECT_ID)
    assert len(background.tasks) == 1, "a failed read demotes no one"
