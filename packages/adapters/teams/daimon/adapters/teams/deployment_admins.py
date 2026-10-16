"""Who administers the deployment from Teams: the admin team's owners.

The owners of `admin_team_id` are admins, read live through the runtime's
short team-owner cache (`TeamMember.Read.Group`), so making someone an owner
needs no redeploy and a removed owner lapses when the cache expires. The
deprecated `admin_user_ids` still counts, on top. Someone from another
organisation (a guest held as one, an external participant, a group chat with
outsiders) is never an admin, and Graph leaves guests out of the owners. A
failed owner read grants nothing and is logged once per lookup.
"""

from __future__ import annotations

import structlog
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.channel_admins import GroupLookupFailed, GroupMembers, ServerAdminIds
from daimon.core.config import TeamsSettings

log = structlog.get_logger()


def admin_team_owners(runtime: TeamsRuntime, *, fresh: bool = False) -> GroupMembers | None:
    """A team's live owners through the shared cache, logging a failed read; None without Graph."""
    lookup = runtime.team_owners
    if lookup is None:
        return None

    async def fetch(group_id: str) -> frozenset[str]:
        try:
            return await lookup(group_id)
        except GroupLookupFailed as exc:
            # Here, not at each check: a cached failure re-raises without a read.
            log.warning("teams.admin_team.lookup_failed", reason=str(exc))
            raise

    async def owners(group_id: str) -> frozenset[str]:
        return await runtime.group_members.members(
            ("teams", group_id), lambda: fetch(group_id), fresh=fresh
        )

    return owners


async def _owner_ids(settings: TeamsSettings, owners: GroupMembers | None) -> frozenset[str]:
    if settings.admin_team_id is None or owners is None:
        return frozenset()
    try:
        return await owners(settings.admin_team_id)
    except GroupLookupFailed:
        return frozenset()  # fail closed; the deprecated list still applies


async def is_deployment_admin(
    settings: TeamsSettings,
    owners: GroupMembers | None,
    *,
    user_id: str,
    is_external: bool,
    home_tenant_id: str | None = None,
) -> bool:
    """Whether `user_id` administers the deployment: listed, or an owner of the admin team."""
    if is_external or (home_tenant_id is not None and home_tenant_id.lower() != settings.tenant_id):
        return False
    user = user_id.lower()
    return user in settings.admin_user_ids or user in await _owner_ids(settings, owners)


async def inbound_is_admin(
    runtime: TeamsRuntime, settings: TeamsSettings, inbound: TeamsInbound
) -> bool:
    """Whether a message's sender administers the deployment, as classified (`is_external`)."""
    return await is_deployment_admin(
        settings,
        admin_team_owners(runtime),
        user_id=inbound.user_id,
        is_external=inbound.is_external,
        home_tenant_id=inbound.home_tenant_id,
    )


async def deployment_admin_ids(
    settings: TeamsSettings, owners: GroupMembers | None
) -> tuple[str, ...]:
    """Every admin, sorted: the admin team's owners and the deprecated list; () for none."""
    return tuple(sorted({*settings.admin_user_ids, *await _owner_ids(settings, owners)}))


def live_admin_ids(runtime: TeamsRuntime) -> ServerAdminIds:
    """The admins a notice to "the admins" reaches, read at each call."""

    async def admin_ids() -> tuple[str, ...]:
        settings = runtime.settings.teams
        if settings is None:
            return ()
        return await deployment_admin_ids(settings, admin_team_owners(runtime))

    return admin_ids


async def boot_admin_ids(runtime: TeamsRuntime, settings: TeamsSettings) -> tuple[str, ...]:
    """The admins at boot, for demoting everyone else; warns once if the old list is set."""
    if settings.admin_user_ids:
        log.warning(
            "teams.admin_user_ids_deprecated",
            count=len(settings.admin_user_ids),
            hint="make these people owners of the DAIMON_TEAMS__ADMIN_TEAM_ID team",
        )
    return await deployment_admin_ids(settings, admin_team_owners(runtime))
