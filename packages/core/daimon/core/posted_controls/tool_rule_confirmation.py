"""Snapshot, authorize and save an Always allow click on a pending card."""

from __future__ import annotations

import asyncio
from typing import Literal

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.confirmation import ConfirmationPrompt, ToolRuleCardReference
from daimon.core.stores import agent_tool_rules
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)
SaveResult = Literal["saved", "stale", "failed"]
RuleScope = Literal["agent", "channel"]
SAVE_TIMEOUT_S = 5.0

ONLY_ADMIN_MESSAGE = "Only an admin can allow this for everyone."
ASK_ADMIN_MESSAGE = "Ask an admin, or use Approve for just this time."
STALE_RULE_MESSAGE = (
    "Approved this time only.\n"
    "Someone changed this tool's setting while the card waited, so Always allow wasn't saved."
)
SAVE_FAILED_MESSAGE = (
    "Approved this time only.\nAlways allow wasn't saved. Try again on the next card."
)


async def snapshot_tool_rule(
    prompt: ConfirmationPrompt, factory: async_sessionmaker[AsyncSession]
) -> ConfirmationPrompt:
    """Read both key revisions before posting; a failed read hides the button."""
    reference = prompt.tool_rule
    if reference is None:
        return prompt
    try:
        async with factory() as session:
            agent = await agent_tool_rules.get_rule(
                session,
                tenant_id=reference.tenant_id,
                agent_id=reference.agent_id,
                tool_key=reference.tool_key,
            )
            channel = (
                await agent_tool_rules.get_rule(
                    session,
                    tenant_id=reference.tenant_id,
                    agent_id=reference.agent_id,
                    tool_key=reference.tool_key,
                    channel_id=reference.channel_id,
                )
                if reference.channel_id is not None
                else None
            )
    except Exception as err:
        _log.warning("tool_confirmation.rule_snapshot_failed", error=str(err))
        return prompt.model_copy(update={"tool_rule": None})
    return prompt.model_copy(
        update={
            "tool_rule": reference.model_copy(
                update={
                    "agent_revision": agent.revision if agent is not None else 0,
                    "channel_revision": channel.revision if channel is not None else 0,
                }
            )
        }
    )


def allowed_rule_scope(reference: ToolRuleCardReference, subject: Subject) -> RuleScope | None:
    """Use the same action checks as direct rule edits; workspace admins win."""
    policy = TenantAccessPolicy()
    if authorize(policy, subject=subject, action=Action.SET_AGENT_TOOL_RULE, place=Place()):
        return "agent"
    if reference.channel_id is not None and authorize(
        policy,
        subject=subject,
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id=reference.channel_id),
    ):
        return "channel"
    return None


async def save_always_allow(
    factory: async_sessionmaker[AsyncSession],
    *,
    reference: ToolRuleCardReference,
    scope: RuleScope,
    subject: Subject,
    clicker: str,
) -> SaveResult:
    """CAS after a won settle; a replay of approval never calls this function."""
    channel_id = reference.channel_id if scope == "channel" else None
    expected_revision = (
        reference.channel_revision if scope == "channel" else reference.agent_revision
    )
    try:
        async with asyncio.timeout(SAVE_TIMEOUT_S):
            async with factory() as session, session.begin():
                await agent_tool_rules.set_rule(
                    session,
                    tenant_id=reference.tenant_id,
                    agent_id=reference.agent_id,
                    agent_name=reference.agent_name,
                    tool_key=reference.tool_key,
                    channel_id=channel_id,
                    server_url=reference.server_url,
                    rule="allow",
                    scope="chat",
                    set_by=clicker,
                    actor=subject,
                    expected_revision=expected_revision,
                )
    except agent_tool_rules.StaleAgentToolRule:
        return "stale"
    except Exception as err:
        _log.warning("tool_confirmation.rule_save_failed", error=str(err))
        return "failed"
    return "saved"
