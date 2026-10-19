"""Drive unattended confirmations for the handle-and-poll MCP turn API.

The session's asking toolsets enforce the gate even if this process stops.
Polling a handle can reattach the bounded worker after a restart. Decisions
and event reduction are shared with the routine turn driver.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import cast

import structlog
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_session_agent_update_param import (
    BetaManagedAgentsSessionAgentUpdateParam,
)
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.resource_ports import (
    list_session_events,
    mcp_scope,
    retrieve_session,
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.session_ports_compat import update_session_record
from daimon.core.tool_safety import (
    daimon_endpoint,
    heal_reserved_server,
    session_tools_for_policy,
    trusted_servers_for,
)
from daimon.core.turn.approvals import (
    agent_rule_lookup,
    build_decision_events,
    daimon_rule_tools_for,
    pending_confirmation_ids,
    tool_calls_for,
    unattended_decider,
)
from daimon.core.turn.ceiling import TURN_CEILING_S
from daimon.core.turn.io import LegacyTurnIO
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState
from mux.contracts.ids import Revision

log = structlog.get_logger(__name__)
_workers: dict[tuple[int, str], asyncio.Task[None]] = {}


async def gate_session(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> None:
    """Gate the existing caller-visible snapshot before a follow-up is sent.

    Updating the frozen arrays preserves hidden servers and caller resources;
    it never restores attachments from the agent's deployment-wide definition.
    """
    policy = runtime.settings.tool_safety
    if not policy.enabled:
        return
    public_url = str(runtime.settings.mcp.public_url) if runtime.settings.mcp.public_url else None
    rules = await daimon_rule_tools_for(
        runtime.session_factory, policy, tenant_id=auth.tenant_id, agent_id=session.agent.id
    )
    tools = session_tools_for_policy(
        policy,
        [t.model_dump(mode="json") for t in session.agent.tools],
        trusted_servers=trusted_servers_for(public_url),
        daimon_rule_tools=rules,
    )
    servers = heal_reserved_server(
        policy,
        [s.model_dump(mode="json") for s in session.agent.mcp_servers],
        public_url=public_url,
    )
    if tools is None and servers is None:
        return
    await update_session_record(
        runtime.client,
        session.id,
        scope=mcp_scope(auth),
        agent_id=session.agent.id,
        revision=Revision(local=session.agent.version, native=str(session.agent.version)),
        agent=cast(
            BetaManagedAgentsSessionAgentUpdateParam,
            {
                "tools": tools
                if tools is not None
                else [t.model_dump(mode="json") for t in session.agent.tools],
                "mcp_servers": servers
                if servers is not None
                else [s.model_dump(mode="json") for s in session.agent.mcp_servers],
            },
        ),
    )


def watch_turn(
    runtime: McpRuntime, auth: AuthIdentity, handle: str, *, turn_event_id: str | None = None
) -> None:
    """Keep one bounded confirmation worker per handle, independent of the request."""
    key = (id(runtime), handle)
    if key in _workers:
        if turn_event_id is None:
            return
        _workers[key].cancel()
    task = asyncio.create_task(
        _watch(runtime, auth, handle, turn_event_id=turn_event_id), context=contextvars.Context()
    )
    _workers[key] = task

    def finished(done: asyncio.Task[None]) -> None:
        if _workers.get(key) is done:
            _workers.pop(key, None)

    task.add_done_callback(finished)


async def _watch(
    runtime: McpRuntime, auth: AuthIdentity, handle: str, *, turn_event_id: str | None = None
) -> None:
    try:
        async with asyncio.timeout(TURN_CEILING_S):
            confirmed: set[str] = set()
            while True:
                session = await retrieve_session(runtime.client, handle, scope=mcp_scope(auth))
                if session.status == "terminated" or session.archived_at is not None:
                    return
                if session.status == "idle":
                    state = TurnState()
                    page: str | None = None
                    # Fetch newest-first until this turn's boundary. Reverse before
                    # folding so a previous turn's end cannot hide the current pause.
                    events: list[BetaManagedAgentsSessionEvent] = []
                    boundary_id: str | None = None
                    for _ in range(20):
                        query: dict[str, object] = {"order": "desc", "limit": 100}
                        if page is not None:
                            query["page"] = page
                        result = await list_session_events(
                            runtime.client, handle, query=query, scope=mcp_scope(auth)
                        )
                        boundary = False
                        for event in result.data:
                            if event.type == "user.message":
                                boundary_id = event.id
                                boundary = True
                                break
                            events.append(event)
                        if boundary or result.next_page is None:
                            break
                        page = result.next_page
                    else:
                        raise RuntimeError("MCP turn history exceeds the confirmation page limit")
                    if turn_event_id is not None and boundary_id != turn_event_id:
                        await asyncio.sleep(1)
                        continue
                    for event in reversed(events):
                        state = apply(state, event)
                        if event.type == "user.tool_confirmation":
                            confirmed.add(event.tool_use_id)
                    ids = pending_confirmation_ids(state.stop_reason, confirmed=confirmed)
                    if ids:
                        policy = runtime.settings.tool_safety
                        public_url = (
                            str(runtime.settings.mcp.public_url)
                            if runtime.settings.mcp.public_url
                            else None
                        )
                        decide = unattended_decider(
                            policy,
                            trusted_servers=trusted_servers_for(public_url),
                            rules_for=agent_rule_lookup(
                                runtime.session_factory,
                                tenant_id=auth.tenant_id,
                                agent_id=session.agent.id,
                                server_urls={s.name: s.url for s in session.agent.mcp_servers},
                                also_urls=daimon_endpoint(public_url),
                            )
                            if policy.enabled and policy.agent_rules
                            else None,
                            daimon_rules=True,
                        )
                        decisions = [
                            (call.tool_use_id, await decide(call))
                            for call in tool_calls_for(state, ids)
                        ]
                        await LegacyTurnIO(runtime.client, handle, scope=mcp_scope(auth)).send(
                            build_decision_events(decisions)
                        )
                        confirmed.update(ids)
                    elif (
                        state.stop_reason is not None
                        and state.stop_reason.type != "requires_action"
                    ):
                        return
                await asyncio.sleep(1)
    except Exception:
        # The session remains gated on any I/O/decider failure. A later poll
        # may reattach; no failure path grants permission.
        log.exception("mcp.agent_chat.confirmation_worker_failed", session_id=handle)


async def pending_turn(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> bool:
    """Hide approval pauses from reply pollers and reattach after a restart."""
    if (id(runtime), session.id) in _workers:
        return True
    if session.status != "idle":
        return False
    result = await list_session_events(
        runtime.client,
        session.id,
        scope=mcp_scope(auth),
        query={"order": "desc", "limit": 1, "types": ["session.status_idle"]},
    )
    if result.data:
        event = result.data[0]
        if event.type == "session.status_idle" and event.stop_reason.type == "requires_action":
            watch_turn(runtime, auth, session.id)
            return True
    return False
