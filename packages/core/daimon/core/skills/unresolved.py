"""A session that can't be created because one of its skills is gone.

Managed Agents rejects a session whose agent pins, or whose channel adds, a
skill (or skill version) that no longer exists: `400 Could not resolve one or
more skills: skill "<id>" version "<v>" not found`. Every turn fails that way
until the reference is removed, so "try again" is the wrong advice. This turns
that rejection into copy that names the skill and says who must remove it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Literal

import anthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsCustomSkill
from daimon.core.errors import UserFacingError

_UNRESOLVED = re.compile(
    r"Could not resolve one or more skills: skill \"(?P<skill_id>[^\"]+)\""
    r"(?: version \"(?P<version>[^\"]*)\")?"
)


class SkillUnavailable(UserFacingError):
    """The session was refused because a skill it mounts no longer exists."""

    def __init__(
        self,
        *,
        skill_id: str,
        label: str,
        attached_to: Literal["agent", "channel"],
        agent_name: str,
    ) -> None:
        self.skill_id = skill_id
        self.label = label
        self.attached_to: Literal["agent", "channel"] = attached_to
        self.agent_name = agent_name
        if attached_to == "channel":
            where = "this channel adds"
            fix = (
                "Ask an admin to remove it from this channel's skills "
                "(remove_channel_skill, or `daimon channels skills remove`), then ask again."
            )
        else:
            where = f"{agent_name} uses"
            fix = (
                f"Ask an admin to remove it from {agent_name}'s skills "
                "(remove_skill), or add it again, then ask again."
            )
        super().__init__(
            f"Daimon couldn't start this conversation: the skill {label} that {where} "
            f"no longer exists. {fix}"
        )


_REASON_MARK = "skill_unavailable "


def encode_reason(error: SkillUnavailable) -> str:
    """A stored-reason form of `error`, so a later refusal can raise it again."""
    fields = {
        "skill_id": error.skill_id,
        "label": error.label,
        "attached_to": error.attached_to,
        "agent_name": error.agent_name,
    }
    return _REASON_MARK + json.dumps(fields, sort_keys=True)


def decode_reason(reason: str | None) -> SkillUnavailable | None:
    """The `SkillUnavailable` a stored reason carries (after any `stage: `), or None."""
    if reason is None:
        return None
    _, _, body = reason.partition(": ")
    if not body.startswith(_REASON_MARK):
        return None
    try:
        fields = json.loads(body[len(_REASON_MARK) :])
    except ValueError:
        return None
    if not isinstance(fields, dict):
        return None
    skill_id, label = fields.get("skill_id"), fields.get("label")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    attached_to, agent_name = fields.get("attached_to"), fields.get("agent_name")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not (
        isinstance(skill_id, str)
        and isinstance(label, str)
        and isinstance(agent_name, str)
        and attached_to in ("agent", "channel")
    ):
        return None
    return SkillUnavailable(
        skill_id=skill_id,
        label=label,
        attached_to="channel" if attached_to == "channel" else "agent",
        agent_name=agent_name,
    )


def unresolved_skill_id(error: BaseException) -> tuple[str, str | None] | None:
    """The skill id (and version) a session create was refused for, or None."""
    if not isinstance(error, anthropic.BadRequestError):
        return None
    match = _UNRESOLVED.search(str(error))
    if match is None:
        return None
    return match.group("skill_id"), match.group("version")


def skill_unavailable(
    *,
    skill_id: str,
    agent: BetaManagedAgentsAgent,
    channel_skills: Sequence[BetaManagedAgentsCustomSkill],
    channel_skill_name: str | None,
) -> SkillUnavailable:
    """The user-facing error for `skill_id`, attributed to the channel or the agent.

    A deleted skill's title is gone from Managed Agents, so a channel skill is
    named from its channel rule and an agent's skill by its id.
    """
    from_channel = any(skill.skill_id == skill_id for skill in channel_skills) and not any(
        skill.skill_id == skill_id for skill in agent.skills
    )
    if from_channel and channel_skill_name:
        label = f'"{channel_skill_name}" ({skill_id})'
    else:
        label = f'"{skill_id}"'
    return SkillUnavailable(
        skill_id=skill_id,
        label=label,
        attached_to="channel" if from_channel else "agent",
        agent_name=agent.name,
    )


__all__ = [
    "SkillUnavailable",
    "decode_reason",
    "encode_reason",
    "skill_unavailable",
    "unresolved_skill_id",
]
