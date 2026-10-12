"""Creating, changing and deleting Spaces, under the policy lock.

A Space is channels of one tenant sharing one audience and one rule, readers
``inside`` or ``own`` (`daimon.core.permissions`). Its rule is also written
onto each member channel's own rule, so a process on a build without Spaces
still keeps every member contained: an agent naming two members has no home
there and is refused, never let out. Server admins and operators only
(`authorize(SET_SPACE)`). Whether two channels really share an audience is the
caller's to confirm; daimon cannot see who reads a channel.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from daimon.core.access_policy import ChannelRule, SpaceRule, TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.permissions import (
    AgentRule,
    RuleRefused,
    channel_rule,
    runs_only_in,
    with_agent_rule,
    with_space,
)
from daimon.core.stores import spaces as store
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    lock_policy_writes_exclusive,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.domain import SpaceRow
from daimon.core.stores.spaces import SpaceRefused
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SpaceChange:
    """What a Space change did."""

    space: SpaceRow
    channel_ids: tuple[str, ...]
    """Its channels afterwards."""
    agents: tuple[str, ...] = ()
    """Agents whose rule the change extended to, or dropped, the channel."""


def _space_rule(space: SpaceRow, channel_ids: Sequence[str]) -> SpaceRule:
    rule = ChannelRule(readers=space.readers, writers=space.writers)
    return SpaceRule(
        key=store.space_key(space.id),
        name=space.name,
        rule=rule,
        channel_ids=frozenset(channel_ids),
    )


def _own_agents(policy: TenantAccessPolicy, space: SpaceRule) -> tuple[str, ...]:
    if space.rule.readers != "own" or not space.channel_ids:
        return ()
    return runs_only_in(policy, min(space.channel_ids))


def join_refusal(policy: TenantAccessPolicy, space: SpaceRule, channel_id: str) -> str | None:
    """Why `channel_id` may not join `space`; None when it may. Pure.

    Two own channels each keeping their own agents would merge two agents'
    memory into one audience, which must be asked for, not implied.
    """
    if space.rule.readers != "own" or channel_rule(policy, channel_id).readers != "own":
        return None
    joining, staying = runs_only_in(policy, channel_id), _own_agents(policy, space)
    if joining and staying and set(joining) != set(staying):
        return (
            f"{channel_id} keeps its own agents ({', '.join(joining)}) and Space {space.name} "
            f"keeps {', '.join(staying)}; release one channel's agents first"
        )
    return None


async def _locked(
    session: AsyncSession, *, tenant_id: uuid.UUID, subject: Subject
) -> TenantAccessPolicy:
    await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    policy = await load_access_policy(session, tenant_id=tenant_id)
    if not authorize(policy, subject=subject, action=Action.SET_SPACE):
        raise SpaceRefused("only a server or workspace admin can change Spaces")
    return policy


async def _space(session: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> SpaceRow:
    space = await store.get_space(session, tenant_id=tenant_id, name=name)
    if space is None:
        raise SpaceRefused(f"there is no Space named {name}")
    return space


async def _members(session: AsyncSession, space: SpaceRow) -> list[str]:
    return [ref.channel_id for ref in await store.list_space_channels(session, space_id=space.id)]


async def _join(
    session: AsyncSession,
    policy: TenantAccessPolicy,
    *,
    space: SpaceRow,
    members: list[str],
    channel_id: str,
    extend_agents: bool,
) -> tuple[TenantAccessPolicy, tuple[str, ...]]:
    before = _space_rule(space, members)
    if refusal := join_refusal(policy, before, channel_id):
        raise SpaceRefused(refusal)
    extended = _own_agents(policy, before) if extend_agents else ()
    await store.add_space_channel(
        session, space=space, tenant_id=space.tenant_id, channel_id=channel_id
    )
    members.append(channel_id)
    try:
        updated = with_space(policy, before.key, _space_rule(space, members))
    except RuleRefused as exc:
        raise SpaceRefused(str(exc)) from exc
    for name in extended:
        runs_in = updated.agent_rules[name].runs_in or ()
        updated = with_agent_rule(updated, name, AgentRule(runs_in=(*runs_in, channel_id)))
    return updated, extended


async def create_space(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    rule: ChannelRule,
    channel_ids: Sequence[str],
    subject: Subject,
) -> SpaceChange:
    """Create Space `name` of `channel_ids`; raise `SpaceRefused` if refused."""
    if rule.readers not in ("inside", "own"):
        raise SpaceRefused("a Space's readers are inside or own")
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await _locked(session, tenant_id=tenant_id, subject=subject)
        space = await store.create_space(session, tenant_id=tenant_id, name=name, rule=rule)
        members: list[str] = []
        updated = with_space(policy, store.space_key(space.id), _space_rule(space, members))
        for channel_id in dict.fromkeys(channel_ids):
            updated, _ = await _join(
                session,
                updated,
                space=space,
                members=members,
                channel_id=channel_id,
                extend_agents=False,
            )
        await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    _log.info("space.created", tenant_id=str(tenant_id), space_id=str(space.id))
    return SpaceChange(space, tuple(members))


async def add_space_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    channel_id: str,
    subject: Subject,
    extend_agents: bool = False,
) -> SpaceChange:
    """Add `channel_id` to Space `name`, its rule becoming the Space's.

    `extend_agents` adds the channel to the rule of each of the Space's own
    agents, so they answer there too.
    """
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await _locked(session, tenant_id=tenant_id, subject=subject)
        space = await _space(session, tenant_id=tenant_id, name=name)
        members = await _members(session, space)
        updated, extended = await _join(
            session,
            policy,
            space=space,
            members=members,
            channel_id=channel_id,
            extend_agents=extend_agents,
        )
        await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    _log.info("space.channel_added", tenant_id=str(tenant_id), space_id=str(space.id))
    return SpaceChange(space, tuple(members), extended)


async def remove_space_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    channel_id: str,
    subject: Subject,
) -> SpaceChange:
    """Take `channel_id` out of Space `name`.

    The channel keeps the Space's rule, so it stays contained. The Space's own
    agents drop it from their rule, keeping the Space as their home; nothing
    is kept to the channel until its rule or an agent's is set again.
    """
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await _locked(session, tenant_id=tenant_id, subject=subject)
        space = await _space(session, tenant_id=tenant_id, name=name)
        members = await _members(session, space)
        if channel_id not in members:
            raise SpaceRefused(f"{channel_id} is not in Space {name}")
        before = _space_rule(space, members)
        own = _own_agents(policy, before)
        await store.remove_space_channel(
            session, space_id=space.id, tenant_id=tenant_id, channel_id=channel_id
        )
        members.remove(channel_id)
        updated = with_space(policy, before.key, _space_rule(space, members))
        dropped: list[str] = []
        for agent in own:
            runs_in = updated.agent_rules[agent].runs_in or ()
            if channel_id in runs_in and len(runs_in) > 1:
                rest = tuple(c for c in runs_in if c != channel_id)
                updated = with_agent_rule(updated, agent, AgentRule(runs_in=rest))
                dropped.append(agent)
        await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    _log.info("space.channel_removed", tenant_id=str(tenant_id), space_id=str(space.id))
    return SpaceChange(space, tuple(members), tuple(dropped))


async def delete_space(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    subject: Subject,
) -> SpaceChange:
    """Delete Space `name`. Its channels keep its rule, each on its own; an agent
    whose rule names several of them has no home left and runs nowhere."""
    async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await _locked(session, tenant_id=tenant_id, subject=subject)
        space = await _space(session, tenant_id=tenant_id, name=name)
        members = await _members(session, space)
        own = _own_agents(policy, _space_rule(space, members))
        stranded = tuple(a for a in own if len(policy.agent_rules[a].runs_in or ()) > 1)
        await store.delete_space(session, space_id=space.id)
    _log.info("space.deleted", tenant_id=str(tenant_id), space_id=str(space.id))
    return SpaceChange(space, tuple(members), stranded)


__all__ = [
    "SpaceChange",
    "SpaceRefused",
    "add_space_channel",
    "create_space",
    "delete_space",
    "join_refusal",
    "remove_space_channel",
]
