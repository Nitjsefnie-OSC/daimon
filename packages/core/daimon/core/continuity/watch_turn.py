"""Read/report authority for page-event turns, independent of agent write grants."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_session_agent_update_param import (
    BetaManagedAgentsSessionAgentUpdateParam,
)
from daimon.core.mcp_server_url import same_mcp_endpoint
from daimon.core.session_ports_compat import update_session_record
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.stores.turn_origins import get_active_origin
from daimon.core.stores.watches import get_firing, get_watch, watch_seed
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy, classify_tool
from daimon.core.turn.admission import Admission
from daimon.core.turn.approvals import RuleLookup, unattended_decider
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from daimon.core.turn.unattended import gate_session_snapshot
from daimon.core.turn_origin import current_execution_origin_id
from mux.contracts.ids import Revision, Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def read_report_policy(policy: ToolSafetyPolicy) -> ToolSafetyPolicy:
    # Retain denials/write pins, never let agent or deployment write grants
    # widen a page-event turn. Read classification is conservative by name.
    return ToolSafetyPolicy(
        enabled=True,
        agent_rules=policy.agent_rules,
        denied=policy.denied,
        effects={key: value for key, value in policy.effects.items() if value == "write"},
    )


def watch_posture(
    policy: ToolSafetyPolicy, *, rules_for: RuleLookup | None = None
) -> PolicyApproval:
    strict = read_report_policy(policy)
    decide = unattended_decider(strict, rules_for=rules_for)

    async def read_only(call: ToolCall) -> ToolConfirmationResult:
        if call.server_name in (None, DAIMON_SERVER_NAME, "sandbox"):
            return ToolConfirmationResult(
                allow=False, deny_message="Watch turns only read and report."
            )
        if classify_tool(strict, server_name=call.server_name, tool_name=call.tool_name) != "read":
            return ToolConfirmationResult(allow=False, deny_message="Watch turns refuse writes.")
        return await decide(call)

    return PolicyApproval(decide=read_only)


async def require_watch_send(
    sessions: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    admission: Admission,
    *,
    session_id: str,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
) -> None:
    origin_id = current_execution_origin_id.get()
    if origin_id is None:
        raise AdmissionDenied(reason="invoker_not_allowed")
    async with sessions() as db:
        seed = await watch_seed(db, row, now=datetime.now(UTC))
        firing = await get_firing(db, row.idempotency_key)
        watch = None if firing is None else await get_watch(db, firing.watch_id)
        origin = await get_active_origin(
            db,
            origin_id=origin_id,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            platform=platform,
            now=datetime.now(UTC),
        )
        live = await get_live_thread_session(
            db,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            account_id=admission.account_id,
        )

    if (
        seed is None
        or origin is None
        or origin.attended
        or not origin.chat_session_id
        or live is None
        or live.ma_session_id != session_id
        or live.effective_config is None
        or live.effective_config.chat_session_id != origin.chat_session_id
        or (origin.parent_channel_id, origin.thread_id, origin.responder_ma_agent_id)
        != (row.parent_channel_id, thread_id, admission.agent.id)
        or watch is None
        or watch.session_id != session_id
        or (
            row.tenant_id,
            row.platform,
            row.thread_id,
            row.requester_account_id,
            row.target_ma_agent_id,
        )
        != (tenant_id, platform, thread_id, admission.account_id, admission.agent.id)
    ):
        raise AdmissionDenied(reason="invoker_not_allowed")


async def constrain_session(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    policy: ToolSafetyPolicy,
    *,
    scope: Scope,
    public_url: str | None,
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    """Remove shell/custom/built-in tools before every send, then force pauses.

    No sandbox toolset, delegation or Daimon management proxy is offered.
    Skills cannot be updated mid-session, so sessions carrying skills are refused.
    The caller must save the original snapshot before mutation and restore it
    before releasing the turn, including when gating or streaming fails.
    """
    if snapshot.agent.skills:
        raise AdmissionDenied(reason="backend_unsupported")
    own_servers = {
        s.name
        for s in snapshot.agent.mcp_servers
        if public_url and same_mcp_endpoint(s.url, public_url)
    }
    tools = [
        tool.model_dump(mode="json")
        for tool in snapshot.agent.tools
        if tool.type == "mcp_toolset"
        and tool.mcp_server_name not in {DAIMON_SERVER_NAME, "sandbox", *own_servers}
    ]
    names = {tool["mcp_server_name"] for tool in tools}
    servers = [
        server.model_dump(mode="json")
        for server in snapshot.agent.mcp_servers
        if server.name in names
    ]
    agent: dict[str, Any] = {"tools": tools, "mcp_servers": servers}
    await update_session_record(
        client,
        snapshot.id,
        scope=scope,
        agent_id=snapshot.agent.id,
        revision=Revision(local=snapshot.agent.version, native=str(snapshot.agent.version)),
        agent=cast(BetaManagedAgentsSessionAgentUpdateParam, agent),
    )
    restricted = snapshot.model_copy(
        update={
            "agent": snapshot.agent.model_copy(
                update={
                    "tools": [
                        tool
                        for tool in snapshot.agent.tools
                        if tool.type == "mcp_toolset" and tool.mcp_server_name in names
                    ],
                    "mcp_servers": [s for s in snapshot.agent.mcp_servers if s.name in names],
                }
            )
        }
    )
    await gate_session_snapshot(
        client,
        restricted,
        read_report_policy(policy),
        scope=scope,
        public_url=public_url,
        session_factory=sessions,
        tenant_id=tenant_id,
    )


async def restore_session_tools(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    *,
    scope: Scope,
) -> None:
    """Finish restoring the original tool configuration even during cancellation.

    Only tool arrays changed by the watch are restored. The persisted snapshot
    has invalidated tool hashes, so a crash or failed provider update forces
    the next preparation to reapply its ordinary tool configuration.
    """
    agent = {
        "tools": [tool.model_dump(mode="json") for tool in snapshot.agent.tools],
        "mcp_servers": [server.model_dump(mode="json") for server in snapshot.agent.mcp_servers],
    }
    restore = asyncio.create_task(
        update_session_record(
            client,
            snapshot.id,
            scope=scope,
            agent_id=snapshot.agent.id,
            revision=Revision(local=snapshot.agent.version, native=str(snapshot.agent.version)),
            agent=cast(BetaManagedAgentsSessionAgentUpdateParam, agent),
        )
    )
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(restore)
            break
        except asyncio.CancelledError as exc:
            if restore.cancelled():
                raise
            # Shutdown and an owner stop can both cancel the same turn. Keep
            # shielding on every request, and propagate only after restoration.
            cancellation = exc
    if cancellation is not None:
        raise cancellation
