"""The one gate for turns nobody is watching.

Two halves, both from the current policy. Before every unattended send,
`gate_session_snapshot` rewrites the session's own tool config so every gated
call pauses: a frozen `always_allow` would otherwise skip the decision
entirely. Every pause is then decided by `unattended_posture`, which always
answers through `unattended_decider` and never `AutoApprove`. Safety off is
the explicit `enabled=False` policy from settings, which the decider reads
as allow.
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
    daimon_endpoint,
    heal_reserved_server,
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
    per-tool config are rewritten, and a foreign `daimon-mcp` URL is healed.
    A failed update raises, so the send never happens on an ungated session.
    """
    tools = [t.model_dump(mode="json") for t in session.agent.tools]
    servers = [s.model_dump(mode="json") for s in session.agent.mcp_servers]
    rules = await daimon_rule_tools_for(
        session_factory, policy, tenant_id=tenant_id, agent_id=session.agent.id
    )
    gated = session_tools_for_policy(
        policy, tools, trusted_servers=trusted_servers_for(public_url), daimon_rule_tools=rules
    )
    healed = heal_reserved_server(policy, servers, public_url=public_url)
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
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    channel_id: str | None = None,
    annotations_for: AnnotationLookup | None = None,
) -> PolicyApproval:
    """How an unattended turn on `session` answers its pauses.

    Always `unattended_decider` over `policy`, with the agent's rules bound to
    the servers this session really talks to. `annotations_for` is the seam
    for operator-trusted MCP hints; it must answer only for trusted servers.
    """
    return PolicyApproval(
        decide=unattended_decider(
            policy,
            trusted_servers=trusted_servers_for(public_url),
            rules_for=agent_rule_lookup(
                session_factory,
                tenant_id=tenant_id,
                agent_id=session.agent.id,
                server_urls={s.name: s.url for s in session.agent.mcp_servers},
                also_urls=daimon_endpoint(public_url),
                channel_id=channel_id,
            ),
            daimon_rules=True,
            annotations_for=annotations_for,
        )
    )
