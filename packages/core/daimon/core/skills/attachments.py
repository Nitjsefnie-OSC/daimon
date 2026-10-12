"""Where a skill is still attached, and the guard that keeps a delete from stranding it.

A session resolves every skill its agent pins and every skill its channel rule
adds when it is created. Deleting a skill one of those still names fails that
session creation (`Could not resolve one or more skills`), and every later
turn with it, until the reference is removed. So a delete first looks for
references: non-archived agents on Managed Agents (skills are workspace-wide,
so any tenant's agent counts) and `channel_skills` rows. With references, the
delete is refused naming them, unless the caller asked to detach everywhere
first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from daimon.core.defaults.ma_index import list_agents_referencing_skill
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.errors import UserFacingError
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.stores.channel_skills import (
    list_skill_channel_rules,
    remove_skill_from_every_channel,
)
from daimon.core.stores.domain import ChannelSkillRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class AgentAttachment:
    agent_id: str
    name: str
    tenant: str | None
    """The agent's `daimon_tenant` metadata, or None for an agent Daimon did not stamp."""


@dataclass(frozen=True)
class SkillAttachments:
    agents: tuple[AgentAttachment, ...]
    channels: tuple[ChannelSkillRow, ...]

    def __bool__(self) -> bool:
        return bool(self.agents or self.channels)


class SkillStillAttached(UserFacingError):
    """A delete was refused because agents or channel rules still use the skill."""

    def __init__(self, message: str, *, attachments: SkillAttachments) -> None:
        super().__init__(message)
        self.attachments = attachments


async def find_skill_attachments(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    skill_id: str,
) -> SkillAttachments:
    """Every non-archived agent pinning `skill_id` and every channel rule adding it."""
    agents = [
        AgentAttachment(
            agent_id=agent.id, name=agent.name, tenant=agent.metadata.get(MA_METADATA_KEY_TENANT)
        )
        for agent in await list_agents_referencing_skill(client, skill_id)
    ]
    async with sessionmaker() as session:
        channels = await list_skill_channel_rules(session, skill_id=skill_id)
    return SkillAttachments(agents=tuple(agents), channels=tuple(channels))


def still_attached_message(
    name: str,
    attachments: SkillAttachments,
    *,
    agent_detach: str,
    channel_detach: str,
    force_hint: str | None,
    viewer_tenant_id: uuid.UUID | None = None,
) -> str:
    """The refusal: where `name` is attached and how to detach it.

    `agent_detach`, `channel_detach` and `force_hint` are the caller's surface's own words
    (a CLI flag, a chat tool), so the steps are ones the reader can take.
    With `viewer_tenant_id`, only that tenant's agents and channels are
    named; references from elsewhere are only counted, so a workspace admin
    never learns another workspace's agent names or channel ids.
    """
    viewer = None if viewer_tenant_id is None else str(viewer_tenant_id)
    places: list[str] = []
    if attachments.agents:
        shown = sorted({a.name for a in attachments.agents if viewer is None or a.tenant == viewer})
        hidden = sum(1 for a in attachments.agents if viewer is not None and a.tenant != viewer)
        parts = list(shown) + ([f"{hidden} agent(s) in another workspace"] if hidden else [])
        places.append(f"agent(s) {', '.join(parts)}")
    if attachments.channels:
        shown_rules = [
            f"{row.platform} channel {row.channel_id}"
            for row in attachments.channels
            if viewer is None or str(row.tenant_id) == viewer
        ]
        hidden = len(attachments.channels) - len(shown_rules)
        parts = shown_rules + ([f"{hidden} channel(s) in another workspace"] if hidden else [])
        places.append(f"channel rule(s) on {', '.join(parts)}")
    steps: list[str] = []
    if attachments.agents:
        steps.append(agent_detach)
    if attachments.channels:
        steps.append(channel_detach)
    message = (
        f"skill {name!r} is still attached to {' and '.join(places)}; deleting it would "
        "fail every turn that uses it. First " + "; ".join(steps) + "."
    )
    if force_hint is not None:
        message += f" {force_hint}"
    return message


async def detach_skill_everywhere(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    skill_id: str,
    attachments: SkillAttachments,
) -> None:
    """Remove `skill_id` from every agent in `attachments` and every channel rule.

    Each agent is re-read before its update (`update_agent_with_version_retry`),
    and keeps its other skills at the versions it pins.
    """
    for attachment in attachments.agents:

        async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
            kept = [
                cast(BetaManagedAgentsSkillParams, skill.model_dump(mode="json"))
                for skill in fresh.skills
                if skill.skill_id != skill_id
            ]
            if len(kept) == len(fresh.skills):
                return fresh
            return await client.beta.agents.update(fresh.id, version=fresh.version, skills=kept)

        await update_agent_with_version_retry(client, attachment.agent_id, _apply)
    async with sessionmaker() as session, session.begin():
        await remove_skill_from_every_channel(session, skill_id=skill_id)


__all__ = [
    "AgentAttachment",
    "SkillAttachments",
    "SkillStillAttached",
    "detach_skill_everywhere",
    "find_skill_attachments",
    "still_attached_message",
]
