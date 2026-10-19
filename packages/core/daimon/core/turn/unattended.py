"""The one gate for turns nobody is watching.

Two halves, both from the current policy. Before every unattended send,
`gate_session_snapshot` rewrites the session's own tool config so every gated
call pauses: a frozen `always_allow` would otherwise skip the decision
entirely. Every pause is then decided by `unattended_posture`, which always
answers through `unattended_decider` and never `AutoApprove`. Safety off is
the explicit `enabled=False` policy from settings, under which the decider
allows ordinary third-party calls.

Daimon's own protected actions (`protected_tools`) ask in these sessions and
their final call is refused, since nobody is there to press the card: a
backend switch and Microsoft sends with safety on or off, Microsoft writes
with it on, the same selection a chat turn asks on.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_session_agent_update_param import (
    BetaManagedAgentsSessionAgentUpdateParam,
)
from daimon.core.session_ports_compat import update_session_record
from daimon.core.tool_safety import (
    ToolSafetyPolicy,
    backend_switch_tools,
    daimon_endpoint,
    heal_reserved_server,
    microsoft_send_tools,
    session_tools_for_policy,
    trusted_servers_for,
)
from daimon.core.turn.approvals import (
    AnnotationLookup,
    agent_rule_lookup,
    daimon_rule_tools_for,
    unattended_decider,
)
from daimon.core.turn.posture import PolicyApproval
from mux.contracts.ids import Revision, Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def protected_tools(policy: ToolSafetyPolicy) -> frozenset[str]:
    """Daimon's tools that act only on a person's card, for an unattended turn.

    Every one a deployment could offer, whether or not this one does: a
    session that asks on a tool it lacks costs nothing. `microsoft_write`
    asks only with the policy on, as it does in chat.
    """
    return backend_switch_tools(enabled=True) | microsoft_send_tools(
        enabled=True, tool_safety_on=policy.enabled
    )


async def gate_session_snapshot(
    client: AsyncAnthropic,
    session: BetaManagedAgentsSession,
    policy: ToolSafetyPolicy,
    *,
    scope: Scope,
    public_url: str | None,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    """Make `session`'s gated calls pause under `policy`, before anything is sent.

    Works from the session's own frozen arrays, so hidden servers and the
    caller's resources stay as they are; nothing is restored from the
    agent's deployment-wide definition. Both the toolset defaults and every
    per-tool config are rewritten, Daimon's `protected_tools` ask, and a
    foreign `daimon-mcp` URL is healed, policy on or off. A failed update
    raises, so the send never happens on an ungated session.
    """
    tools = [t.model_dump(mode="json") for t in session.agent.tools]
    servers = [s.model_dump(mode="json") for s in session.agent.mcp_servers]
    rules = await daimon_rule_tools_for(
        session_factory, policy, tenant_id=tenant_id, agent_id=session.agent.id
    )
    protected = protected_tools(policy)
    gated = session_tools_for_policy(
        policy,
        tools,
        trusted_servers=trusted_servers_for(public_url),
        confirmed_tools=protected,
        daimon_rule_tools=rules,
    )
    healed = heal_reserved_server(policy, servers, public_url=public_url, confirmed_tools=protected)
    if gated is None and healed is None:
        return
    agent: dict[str, Any] = {
        "tools": gated if gated is not None else tools,
        "mcp_servers": healed if healed is not None else servers,
    }
    await update_session_record(
        client,
        session.id,
        scope=scope,
        agent_id=session.agent.id,
        revision=Revision(local=session.agent.version, native=str(session.agent.version)),
        agent=cast(BetaManagedAgentsSessionAgentUpdateParam, agent),
    )


def unattended_posture(
    policy: ToolSafetyPolicy,
    *,
    session: BetaManagedAgentsSession,
    public_url: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    tenant_id: uuid.UUID | None,
    channel_id: str | None = None,
    annotations_for: AnnotationLookup | None = None,
) -> PolicyApproval:
    """How an unattended turn on `session` answers its pauses.

    Always `unattended_decider` over `policy`, with the agent's rules bound to
    the servers this session really talks to and to `channel_id`, the
    channel whose rules the turn runs under (`channel_for_tool_rules`; None
    for a DM or no channel); with no database or tenant there are no rules to
    read. `protected_tools` are refused. `annotations_for`
    is the seam for operator-trusted MCP hints; it must answer only for
    trusted servers.
    """
    return PolicyApproval(
        decide=unattended_decider(
            policy,
            trusted_servers=trusted_servers_for(public_url),
            confirmed_tools=protected_tools(policy),
            rules_for=agent_rule_lookup(
                session_factory,
                tenant_id=tenant_id,
                agent_id=session.agent.id,
                server_urls={s.name: s.url for s in session.agent.mcp_servers},
                also_urls=daimon_endpoint(public_url),
                channel_id=channel_id,
            )
            if session_factory is not None and tenant_id is not None
            else None,
            daimon_rules=True,
            annotations_for=annotations_for,
        )
    )
