"""One lock per skill id that serializes deleting a skill against attaching it.

A delete checks that nothing references a skill and then deletes it on Managed
Agents; an attach checks that the skill exists and then writes a reference (an
agent's `skills` list or a `channel_skills` row). Without coordination an
attach landing between the delete's check and its provider delete leaves a
reference to a skill that no longer exists, and every turn that mounts it
fails. So both sides take a Postgres transaction-scoped advisory lock on the
skill id: a delete takes it exclusively and holds it from its final reference
check through the provider delete; an attach takes it shared and holds it from
its existence check until its reference is written (or, for a `channel_skills`
row, until the row's transaction commits). Attaches never block each other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.errors import UserFacingError
from daimon.core.mux_compat import retrieve_skill, update_agent
from mux.contracts.ids import Scope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NAMESPACE = "skill_reference"


class SkillDeletedWhileAttaching(UserFacingError):
    """The skill being attached was deleted before the reference could be written."""

    def __init__(self, skill_id: str) -> None:
        super().__init__(
            f"The skill {skill_id} was deleted just now, so it was not attached. "
            "Add the skill again, then attach it."
        )
        self.skill_id = skill_id


def _key(skill_id: str) -> str:
    return f"{_NAMESPACE}:{skill_id}"


async def lock_skill_for_delete(session: AsyncSession, skill_id: str) -> None:
    """Take the exclusive lock on `skill_id` for the rest of `session`'s transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": _key(skill_id)}
    )


async def lock_skills_for_attach(
    session: AsyncSession,
    client: AsyncAnthropic,
    skill_ids: Iterable[str],
    *,
    backend_scope: Scope | None = None,
) -> None:
    """Take the shared lock on each id, then check each skill still exists.

    Ids are locked in sorted order so two writers never deadlock. Raises
    `SkillDeletedWhileAttaching` when one is gone: a delete that held the
    exclusive lock finished first, and attaching it now would strand the
    agent or channel. `backend_scope` is the tenant scope of the existence
    check; without it the check runs under the legacy host scope.
    """
    scope = backend_scope or Scope.legacy_host_authorized(
        call_site="daimon.core.skills.reference_lock:lock_skills_for_attach"
    )
    ordered = sorted(set(skill_ids))
    for skill_id in ordered:
        await session.execute(
            text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
            {"key": _key(skill_id)},
        )
    for skill_id in ordered:
        try:
            await retrieve_skill(client, skill_id, scope=scope)
        except anthropic.NotFoundError as err:
            raise SkillDeletedWhileAttaching(skill_id) from err


ReferenceScope = async_sessionmaker[AsyncSession] | AsyncSession
"""Where an attach takes its shared lock: a sessionmaker opens a short transaction
around the write; an open session (the defaults reconcile's lock session, so a
one-connection pool is never checked out twice) holds it until that session's
transaction ends."""


@asynccontextmanager
async def skill_attach_guard(
    scope: ReferenceScope | None,
    client: AsyncAnthropic,
    skill_ids: Iterable[str],
    *,
    backend_scope: Scope | None = None,
) -> AsyncIterator[None]:
    """Hold the shared lock on `skill_ids`, checked to exist, around a reference write.

    No ids is a no-op. `scope=None` is only for a caller with no database
    (unit tests); every production writer passes one. `backend_scope` is the
    tenant scope of the existence check (`lock_skills_for_attach`).
    """
    ids = sorted(set(skill_ids))
    if not ids or scope is None:
        yield
        return
    if isinstance(scope, AsyncSession):
        await lock_skills_for_attach(scope, client, ids, backend_scope=backend_scope)
        yield
        return
    async with scope() as session, session.begin():
        await lock_skills_for_attach(session, client, ids, backend_scope=backend_scope)
        yield


def _custom_ids(skills: Iterable[Any]) -> set[str]:
    ids: set[str] = set()
    for skill in skills:
        if isinstance(skill, Mapping):
            kind = skill.get("type")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            skill_id = skill.get("skill_id")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        else:
            kind = getattr(skill, "type", None)
            skill_id = getattr(skill, "skill_id", None)
        if kind != "anthropic" and isinstance(skill_id, str):
            ids.add(skill_id)
    return ids


def added_custom_skill_ids(*, before: Sequence[Any], after: Iterable[Any]) -> set[str]:
    """Custom skill ids in `after` that `before` did not reference.

    Accepts SDK skill models or their dict form; built-in (`anthropic`) skills
    are never deleted by Daimon, so they need no lock.
    """
    return _custom_ids(after) - _custom_ids(before)


async def guarded_agent_update(
    client: AsyncAnthropic,
    scope: ReferenceScope | None,
    fresh: BetaManagedAgentsAgent,
    *,
    backend_scope: Scope | None = None,
    **fields: Any,  # noqa: ANN401
) -> BetaManagedAgentsAgent:
    """Update `fresh` to `fields` at `fresh.version`, under the attach lock.

    Only skills `fields["skills"]` adds to `fresh` are locked and checked; an
    update that adds none touches no database. `backend_scope` is the tenant
    scope of the existence check and the update; without it both run under
    the legacy host scope.
    """
    backend = backend_scope or Scope.legacy_host_authorized(
        call_site="daimon.core.skills.reference_lock:guarded_agent_update"
    )
    added = added_custom_skill_ids(before=fresh.skills, after=fields.get("skills") or [])
    async with skill_attach_guard(scope, client, added, backend_scope=backend):
        return await update_agent(
            client, fresh.id, version=fresh.version, payload=fields, scope=backend
        )


__all__ = [
    "ReferenceScope",
    "SkillDeletedWhileAttaching",
    "added_custom_skill_ids",
    "guarded_agent_update",
    "lock_skill_for_delete",
    "lock_skills_for_attach",
    "skill_attach_guard",
]
