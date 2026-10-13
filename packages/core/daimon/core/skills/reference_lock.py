"""`guarded_agent_update`, kept only for callers that still import it.

Skill deletes check for references in one place
(`daimon.core.skills.attachments.delete_skill_unless_attached`); attaches
take no lock. Remove this module once nothing imports it.
"""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent


async def guarded_agent_update(
    client: AsyncAnthropic,
    _scope: object,
    fresh: BetaManagedAgentsAgent,
    **fields: Any,  # noqa: ANN401
) -> BetaManagedAgentsAgent:
    """`agents.update(fresh.id, version=fresh.version, **fields)`; `_scope` is ignored."""
    return await client.beta.agents.update(fresh.id, version=fresh.version, **fields)


__all__ = ["guarded_agent_update"]
