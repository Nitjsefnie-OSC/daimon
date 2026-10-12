"""Client-side LIST filters for finding daimon-tagged resources on MA.

Agents and environments are matched by metadata (daimon_tenant +
daimon_name). Skills are matched by display_title — MA skill list items
do not expose metadata.

On multi-match, the most recently created resource is adopted; the
others are left alone and a warning is logged.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Iterable, Mapping
from typing import Literal

import sentry_sdk
import structlog
from anthropic import AsyncAnthropic, omit
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSkillParams,
    SkillListResponse,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
    find_conflicting_skill_body,
    find_mount_collision,
    strip_tenant_prefix,
    tenant_scoped_display_title,
)
from daimon.core.errors import DaimonError, SkillsListTruncatedError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skills.reference_lock import ReferenceScope
from daimon.core.stores.tenants import list_tenant_ids_with_prefixes
from sqlalchemy.ext.asyncio import AsyncSession

# A title stamped before titles carried the whole tenant id: 8 hex chars, "-",
# the body. A full-id title has a hex char where this one has its "-".
_SHORT_PREFIX_TITLE = re.compile(r"([0-9a-f]{8})-(.+)", re.DOTALL)

_log = structlog.get_logger(__name__)


class SkillVersionNotFoundError(DaimonError):
    """A skill has no version with the asked-for epoch string."""


# Anthropic documents a `page` / `next_page` cursor for skills. Older live
# responses omitted `next_page` even at a page boundary (verified 2026-06-10).
# Follow a cursor when present, but treat a full final page without one as
# potentially truncated so create/delete decisions remain safe.
#
# Raised 100 -> 1000 after production crossed 100 skills and locked itself out
# of every write path: the seeded-agent reconcile could not resolve a skill id,
# `defaults verify` reported every tenant unverifiable, the smoke turn could not
# resolve its agent, and a fresh install would have failed to provision at all.
# `skills.list` honours a larger limit — 103 rows came back with has_more=False
# at limit=200 against production, verified live 2026-08-08 — so the ceiling was
# ours, not the API's. The guard remains necessary if the API omits the cursor.
_SKILLS_PAGE_LIMIT = 1000


async def find_agents_by_daimon_tag(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    name: str,
    include_archived: bool = False,
) -> list[BetaManagedAgentsAgent]:
    """Return all MA agents tagged `(tenant_id, name)`, canonical first.

    The first element (max `created_at`) is the canonical match the resolver
    and reconcile should adopt; remaining elements are duplicates that
    reconcile is responsible for archiving (per R5 dedup). Empty list = no
    match.

    `include_archived=True` lifts the archived filter — useful for `daimon
    agents get --include-archived` when an operator needs to inspect an
    archived row. Defaults to False so reconcile + the resolver never adopt
    archived agents.
    """
    matches: list[BetaManagedAgentsAgent] = []
    async for ag in client.beta.agents.list(include_archived=include_archived):
        if (
            ag.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id)
            and ag.metadata.get(MA_METADATA_KEY_NAME) == name
        ):
            matches.append(ag)
    if len(matches) > 1:
        _log.warning("ma_index.multi_match", kind="agents", count=len(matches))
    matches.sort(key=lambda m: m.created_at, reverse=True)
    return matches


async def find_agent_by_daimon_tag(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    name: str,
    include_archived: bool = False,
) -> BetaManagedAgentsAgent | None:
    """Read-only adapter for the resolver: canonical match only, no side effects."""
    matches = await find_agents_by_daimon_tag(
        client, tenant_id=tenant_id, name=name, include_archived=include_archived
    )
    if len(matches) > 1:
        # Account-aware tripwire. The ambiguous state is degraded but not
        # an outage — adoption still proceeds with the newest match. The warning
        # makes the cross-account collision diagnosable without causing downtime.
        _log.warning(
            "ma_index.resolver_ambiguous_name",
            tenant_id=str(tenant_id),
            name=name,
            count=len(matches),
            adopted_id=matches[0].id,
            adopted_account=matches[0].metadata.get(MA_METADATA_KEY_ACCOUNT),
            duplicate_accounts=[m.metadata.get(MA_METADATA_KEY_ACCOUNT) for m in matches[1:]],
        )
    return matches[0] if matches else None


async def find_environments_by_daimon_tag(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, name: str
) -> list[BetaEnvironment]:
    """Parity with `find_agents_by_daimon_tag` — canonical first, duplicates follow."""
    matches: list[BetaEnvironment] = []
    async for env in client.beta.environments.list(include_archived=False):
        if (
            env.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id)
            and env.metadata.get(MA_METADATA_KEY_NAME) == name
        ):
            matches.append(env)
    if len(matches) > 1:
        _log.warning("ma_index.multi_match", kind="environments", count=len(matches))
    matches.sort(key=lambda m: m.created_at, reverse=True)
    return matches


async def find_environment_by_daimon_tag(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, name: str
) -> BetaEnvironment | None:
    """Read-only adapter for the resolver: canonical match only, no side effects."""
    matches = await find_environments_by_daimon_tag(client, tenant_id=tenant_id, name=name)
    return matches[0] if matches else None


async def list_agents_by_tenant(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID
) -> list[BetaManagedAgentsAgent]:
    """Return all non-archived MA agents tagged with tenant_id."""
    results: list[BetaManagedAgentsAgent] = []
    async for ag in client.beta.agents.list(include_archived=False):
        if ag.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id):
            results.append(ag)
    return results


async def list_agents_for_privacy_deletion(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID
) -> list[BetaManagedAgentsAgent]:
    """Return the tenant's live and archived agents for transcript deletion."""
    results: list[BetaManagedAgentsAgent] = []
    async for agent in client.beta.agents.list(include_archived=True):
        if agent.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id):
            results.append(agent)
    return results


async def list_agents_by_tenants(
    client: AsyncAnthropic, *, tenant_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[BetaManagedAgentsAgent]]:
    """Return non-archived MA agents for several tenants from one org listing.

    The org listing is paginated in full whichever way it is filtered, so a
    caller holding many tenants (a hub login spanning several workspaces)
    pays once here rather than once per tenant. Every requested tenant is a
    key in the result, empty when it has no agents.
    """
    wanted = {str(t): t for t in tenant_ids}
    results: dict[uuid.UUID, list[BetaManagedAgentsAgent]] = {t: [] for t in tenant_ids}
    if not wanted:
        return results
    async for ag in client.beta.agents.list(include_archived=False):
        tenant = wanted.get(ag.metadata.get(MA_METADATA_KEY_TENANT, ""))
        if tenant is not None:
            results[tenant].append(ag)
    return results


async def find_agent_by_derived_uuid(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> BetaManagedAgentsAgent | None:
    """Return the tenant's MA agent whose derived uuid5 is ``agent_id``, or None.

    The inverse of ``derive_agent_uuid``, which is one-way — so this scans the
    tenant's agents and re-derives. Needed by callers that hold only daimon's
    derived id (rows in ``credential_requests`` and other daimon-side tables
    key on it, never on the MA id) but must act on the MA agent itself.
    """
    for agent in await list_agents_by_tenant(client, tenant_id=tenant_id):
        if derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)) == agent_id:
            return agent
    return None


async def list_referenced_skill_ids(client: AsyncAnthropic) -> set[str]:
    """Return the skill ids pinned by any non-archived agent (org-wide).

    Skills are org-wide on MA and carry no metadata field, so the skill sweep
    cannot distinguish a defaults-created skill from a user-synced one the way
    the agent/env sweeps use the `daimon_managed` marker. Sparing any skill an
    agent still references is the available proxy for "in use, don't delete"
    and prevents the sweep from deleting a live agent's skill out from under it
    (smoke-matrix #19). Not tenant-scoped: skills are org-wide, so a reference
    from an agent in any tenant counts.
    """
    referenced: set[str] = set()
    async for ag in client.beta.agents.list(include_archived=False):
        for skill in ag.skills:
            referenced.add(skill.skill_id)
    return referenced


async def list_agents_referencing_skill(
    client: AsyncAnthropic, skill_id: str
) -> list[BetaManagedAgentsAgent]:
    """Every non-archived agent pinning `skill_id`, in any tenant (org-wide).

    The cross-tenant read is the point: skills are org-wide, so a delete must
    see every agent that would fail without the skill. Callers decide which of
    these agents a reader may see named.
    """
    return [
        ag
        async for ag in client.beta.agents.list(include_archived=False)
        if any(skill.skill_id == skill_id for skill in ag.skills)
    ]


async def list_environments_by_tenant(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID
) -> list[BetaEnvironment]:
    """Return all non-archived MA environments tagged with tenant_id."""
    results: list[BetaEnvironment] = []
    async for env in client.beta.environments.list(include_archived=False):
        if env.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id):
            results.append(env)
    return results


async def _owned_titles(
    rows: list[SkillListResponse], tenants: ReferenceScope | None
) -> list[SkillListResponse]:
    """Read each short-prefix title as its tenant's full-id title, when it has one.

    A title stamped with 8 hex chars of a tenant id is that tenant's only when
    exactly one tenant of this deployment has those 8 chars: its row is read
    under the title `tenant_scoped_display_title` gives it now, so ownership
    (`strip_tenant_prefix`) and lookups by name treat it like any other. One
    prefix several tenants share is nobody's, and is logged. One bounded read
    of the tenants table (on `tenants`), only when such a title is listed;
    with no `tenants` such a title stays nobody's.
    """
    short = {
        row.id: match
        for row in rows
        if row.source == "custom"
        and (match := _SHORT_PREFIX_TITLE.fullmatch(row.display_title or "")) is not None
    }
    if not short or tenants is None:
        return rows
    prefixes = {match.group(1) for match in short.values()}
    if isinstance(tenants, AsyncSession):
        ids = await list_tenant_ids_with_prefixes(tenants, prefixes=prefixes)
    else:
        async with tenants() as session:
            ids = await list_tenant_ids_with_prefixes(session, prefixes=prefixes)
    owners: dict[str, list[uuid.UUID]] = {p: [] for p in prefixes}
    for tenant_id in ids:
        owners[str(tenant_id)[:8]].append(tenant_id)
    owned: list[SkillListResponse] = []
    for row in rows:
        match = short.get(row.id)
        if match is not None and len(owners[match.group(1)]) == 1:
            agent_name, sep, name = match.group(2).partition("/")
            title = tenant_scoped_display_title(
                tenant_id=owners[match.group(1)][0],
                name=name if sep else match.group(2),
                agent_name=agent_name if sep else None,
            )
            row = row.model_copy(update={"display_title": title})
        elif match is not None and len(owners[match.group(1)]) > 1:
            _log.warning("ma_index.skill_prefix_ambiguous", skill_id=row.id)
        owned.append(row)
    return owned


async def _collect_skills_page(
    client: AsyncAnthropic, tenants: ReferenceScope | None = None
) -> tuple[list[SkillListResponse], bool]:
    """Follow skill cursors and flag a full terminal page with no cursor."""
    rows: list[SkillListResponse] = []
    page = await client.beta.skills.list(limit=_SKILLS_PAGE_LIMIT)
    truncated = False
    async for current in page.iter_pages():
        rows.extend(current.data)
        if len(current.data) >= _SKILLS_PAGE_LIMIT and not current.next_page:
            truncated = True
    return await _owned_titles(rows, tenants), truncated


async def list_skills_strict(
    client: AsyncAnthropic, *, tenants: ReferenceScope | None = None
) -> list[SkillListResponse]:
    """Return all MA skills, raising if the last full page has no cursor.

    Use in write contexts (create, delete, dedup) where making decisions on a
    truncated view is unsafe. `tenants` lets a short-prefix title be read as
    its tenant's (`_owned_titles`).
    """
    rows, page_full = await _collect_skills_page(client, tenants)
    if page_full:
        raise SkillsListTruncatedError(
            f"skills.list returned a full page of {_SKILLS_PAGE_LIMIT} rows — "
            "the org skill view may be "
            "truncated; create/delete decisions on this view are unsafe"
        )
    return rows


async def download_skill_version(client: AsyncAnthropic, *, skill_id: str, version: str) -> bytes:
    """Return the zip of `skill_id` at `version` (the epoch version string).

    The SDK's download sends `anthropic-beta: skills-2025-10-02`, which a
    workspace API key is refused with 403; without it the endpoint takes the
    version's id, not its epoch string (verified against production 2026-10-09).
    """
    version_id = None
    async for row in client.beta.skills.versions.list(skill_id=skill_id):
        if row.version == version:
            version_id = row.id
            break
    if version_id is None:
        raise SkillVersionNotFoundError(f"{skill_id} has no version {version}.")
    content = await client.beta.skills.versions.download(
        version_id, skill_id=skill_id, extra_headers={"anthropic-beta": omit}
    )
    try:
        return await content.read()
    finally:
        await content.close()


async def list_skills_lenient(
    client: AsyncAnthropic, *, tenants: ReferenceScope | None = None
) -> tuple[list[SkillListResponse], bool]:
    """Return (rows, truncated) for the MA skills page.

    When truncated: emits a structlog warning and a Sentry capture. Use in read
    contexts where degraded results are acceptable but should be observable.
    """
    rows, page_full = await _collect_skills_page(client, tenants)
    if page_full:
        _log.warning("ma_index.skills_list_truncated", limit=_SKILLS_PAGE_LIMIT)
        sentry_sdk.capture_message("skills list truncated at MA API page limit", level="warning")
    return rows, page_full


async def find_skills_by_display_title(
    client: AsyncAnthropic,
    display_title: str,
    *,
    on_truncation: Literal["raise", "degrade"],
    tenants: ReferenceScope | None = None,
) -> list[SkillListResponse]:
    """Return all custom MA skills with `display_title`, canonical first.

    Parity with `find_agents_by_daimon_tag` — the first element (max
    `created_at`) is the canonical match the resolver and reconcile should
    adopt; remaining elements are duplicates that reconcile is responsible for
    cleaning up. Empty list = no match.

    Filters out ``source="anthropic"`` so built-in skills can never collide with
    a user-defined display_title.

    ``on_truncation="raise"`` raises SkillsListTruncatedError when the page is
    full REGARDLESS of whether a match was found — a match on a truncated view
    can still hide duplicates (write mode). ``on_truncation="degrade"``
    logs and captures to Sentry but returns matches (read mode). Callers
    must choose explicitly: write/create/delete contexts use "raise"; read
    contexts use "degrade".
    """
    rows, page_full = await _collect_skills_page(client, tenants)
    if page_full:
        if on_truncation == "raise":
            raise SkillsListTruncatedError(
                f"skills.list returned a full page of {_SKILLS_PAGE_LIMIT} rows — "
                "the org skill view may be "
                "truncated; create/delete decisions on this view are unsafe"
            )
        _log.warning("ma_index.skills_list_ceiling_hit", limit=_SKILLS_PAGE_LIMIT)
        sentry_sdk.capture_message("skills list truncated at MA API page limit", level="warning")
    return match_skills_by_display_title(rows, display_title)


def match_skills_by_display_title(
    rows: list[SkillListResponse], display_title: str
) -> list[SkillListResponse]:
    """Find canonical and duplicate custom skills in an already validated view."""
    matches = [sk for sk in rows if sk.source == "custom" and sk.display_title == display_title]
    if len(matches) > 1:
        _log.warning("ma_index.multi_match", kind="skills", count=len(matches))
    matches.sort(key=lambda m: m.created_at, reverse=True)
    return matches


async def skill_mount_bodies(
    client: AsyncAnthropic,
    rows: Mapping[str, SkillListResponse],
    skill_ids: Iterable[str],
    *,
    tenant_id: uuid.UUID,
) -> dict[str, str]:
    """Title body of each custom skill in `skill_ids`, its last segment the folder it mounts under.

    For mount checks only, so a skill counts whether or not it is the tenant's:
    one already on an agent mounts there regardless. The body is read off the
    title unless that may be wrong, when the name comes from the skill's latest
    version: a title shortened to fit MA's 64 chars (a "~", which a skill name
    never holds, marks it) may have cut it, and a title the tenant doesn't own
    gives no body. A shortened body cut before its "/" keeps only the start of
    a long library name or of a long agent name; it is the agent's when the
    name does not begin with it.
    """
    out: dict[str, str] = {}
    for skill_id in skill_ids:
        row = rows.get(skill_id)
        if row is None or row.source != "custom":
            continue
        body = strip_tenant_prefix(tenant_id=tenant_id, display_title=row.display_title or "")
        if body is None or "~" in body:
            mount = (body or row.id).rsplit("/", 1)[-1]
            async for version in client.beta.skills.versions.list(skill_id=row.id):
                mount = version.name
                if version.version == row.latest_version:
                    break
            head = (body or "").rpartition("/")[0]
            if body is not None and not head and not mount.startswith(body[: body.rfind("~")]):
                head = body[: body.rfind("~")]
            body = f"{head}/{mount}" if head else mount
        out[skill_id] = body
    return out


async def find_conflicting_skill_mount(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    name: str,
    agent_name: str | None,
    tenants: ReferenceScope | None = None,
) -> SkillListResponse | None:
    """Return a tenant skill whose MA mount name would collide with creating
    ``name`` under the given shape (registry when ``agent_name`` is None,
    agent-scoped otherwise), else None.

    MA mounts by the skill's internal name at SESSION create, not at skill
    create — so a registry skill and an agent-scoped skill sharing a terminal
    name are both accepted on upload and only fail once attached to the same
    agent. Call this before ``skills.create`` in write contexts; collision
    rules live in :func:`~daimon.core.defaults.metadata.find_conflicting_skill_body`.

    Truncation always raises SkillsListTruncatedError — this is a create-guard,
    and a truncated view can hide the collider.
    """
    rows = {sk.id: sk for sk in await list_skills_strict(client, tenants=tenants)}
    owned = [
        sk.id
        for sk in rows.values()
        if strip_tenant_prefix(tenant_id=tenant_id, display_title=sk.display_title or "")
        is not None
    ]
    bodies = await skill_mount_bodies(client, rows, owned, tenant_id=tenant_id)
    by_body = {body: rows[skill_id] for skill_id, body in bodies.items()}
    conflict = find_conflicting_skill_body(
        existing_bodies=list(by_body), name=name, agent_name=agent_name
    )
    return by_body[conflict] if conflict is not None else None


async def find_attach_mount_collision(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    skills: list[BetaManagedAgentsSkillParams],
    tenants: ReferenceScope | None = None,
) -> str | None:
    """Return a human-readable description of a mount-name collision in a
    proposed agent skill list, else None.

    Attach-side companion to :func:`find_conflicting_skill_mount`: the create
    guards stop NEW colliding skills, but attaching an EXISTING registry skill
    to an agent that owns a same-named agent-scoped skill would still brick
    session creation. Call this on the merged list before ``agents.update``.

    Truncation raises SkillsListTruncatedError — a truncated view can hide the
    collider, and attaching blind bricks the agent.
    """
    rows = {sk.id: sk for sk in await list_skills_strict(client, tenants=tenants)}
    skill_ids = [s["skill_id"] for s in skills]
    collision = find_mount_collision(
        skill_ids=skill_ids,
        body_by_skill_id=await skill_mount_bodies(client, rows, skill_ids, tenant_id=tenant_id),
    )
    if collision is None:
        return None
    mount_name, bodies_at = collision
    return (
        f"skills {bodies_at!r} would all mount at {mount_name!r} on this agent — "
        f"rename one (e.g. {mount_name}-2) and re-sync before attaching"
    )


async def find_skill_by_display_title(
    client: AsyncAnthropic,
    display_title: str,
    *,
    on_truncation: Literal["raise", "degrade"],
    tenants: ReferenceScope | None = None,
) -> SkillListResponse | None:
    """Read-only adapter for callers that only need the canonical match."""
    matches = await find_skills_by_display_title(
        client, display_title, on_truncation=on_truncation, tenants=tenants
    )
    return matches[0] if matches else None
