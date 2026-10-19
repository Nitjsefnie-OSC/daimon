"""A watch turn runs only on a session whose every capability-bearing field is
in an explicit allowlist. Any field that is set but not allowlisted (skills,
multiagent, or anything added later) refuses the turn before any provider
mutation. Tools are kept by allowlist (reviewed, explicitly named read tools),
not removed by denylist. Unlisted tools are disabled, and every retained tool
must pause for the current read/report decision."""

from __future__ import annotations

import asyncio
import re
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
from daimon.core.turn_origin import current_execution_origin_id
from mux.contracts.ids import Revision, Scope
from pydantic import BaseModel
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


def _only_fields(value: BaseModel, names: str) -> None:
    # Freeze the reviewed schema independently of the installed SDK. Check SDK
    # fields as well as response extras: an SDK upgrade cannot silently grant
    # a new capability that the response parser now recognizes.
    present = set(type(value).model_fields) | set(value.model_extra or {}) | set(vars(value))
    # The SDK attaches this diagnostic attribute outside the response schema.
    if present - set(names.split()) - {"_request_id"}:
        raise AdmissionDenied(reason="backend_unsupported")


def validate_watch_snapshot(snapshot: BetaManagedAgentsSession) -> None:
    """Pure validation, before any update or provider restoration is registered."""
    _only_fields(
        snapshot,
        """
        id agent archived_at created_at environment_id metadata outcome_evaluations
        resources stats status title type updated_at usage vault_ids deployment_id
    """,
    )
    _only_fields(snapshot.stats, "active_seconds duration_seconds")
    _only_fields(
        snapshot.usage, "cache_creation cache_read_input_tokens input_tokens output_tokens"
    )
    if snapshot.usage.cache_creation is not None:
        _only_fields(
            snapshot.usage.cache_creation, "ephemeral_1h_input_tokens ephemeral_5m_input_tokens"
        )
    for outcome in snapshot.outcome_evaluations:
        _only_fields(
            outcome, "completed_at description explanation iteration outcome_id result type"
        )
    agent = snapshot.agent
    _only_fields(
        agent,
        """
        id description mcp_servers model multiagent name skills system tools type version
    """,
    )
    if agent.skills or agent.multiagent is not None:
        raise AdmissionDenied(reason="backend_unsupported")
    _only_fields(agent.model, "id speed")
    for server in agent.mcp_servers:
        _only_fields(server, "name type url")
        if server.type != "url":
            raise AdmissionDenied(reason="backend_unsupported")
    if len({s.name for s in agent.mcp_servers}) != len(agent.mcp_servers):
        raise AdmissionDenied(reason="backend_unsupported")
    # Mounts and vault IDs supply data/credentials, not execution. Native tools
    # are removed below; only the named MCP read allowlist can use credentials.
    for resource in snapshot.resources:
        if resource.type == "file":
            _only_fields(resource, "id created_at file_id mount_path type updated_at")
        elif resource.type == "github_repository":
            _only_fields(resource, "id created_at mount_path type updated_at url checkout")
            if resource.checkout is not None:
                _only_fields(
                    resource.checkout,
                    "type name" if resource.checkout.type == "branch" else "type sha",
                )
        elif resource.type == "memory_store":
            _only_fields(
                resource, "memory_store_id type access description instructions mount_path name"
            )
        else:
            raise AdmissionDenied(reason="backend_unsupported")
    for tool in agent.tools:
        if tool.type == "custom":
            _only_fields(tool, "description input_schema name type")
            continue  # Entire custom tool, including its opaque JSON schema, is removed.
        if tool.type not in ("mcp_toolset", "agent_toolset_20260401"):
            raise AdmissionDenied(reason="backend_unsupported")
        _only_fields(
            tool,
            "configs default_config type"
            + (" mcp_server_name" if tool.type == "mcp_toolset" else ""),
        )
        _only_fields(tool.default_config, "enabled permission_policy")
        _only_fields(tool.default_config.permission_policy, "type")
        if len({c.name for c in tool.configs}) != len(tool.configs):
            raise AdmissionDenied(reason="backend_unsupported")
        for config in tool.configs:
            _only_fields(config, "enabled name permission_policy")
            _only_fields(config.permission_policy, "type")


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
    """Install one explicit read allowlist; never enable an entire toolset.

    The snapshot must enumerate enabled read tools in per-tool configs. Empty
    or wildcard/default-only toolsets grant nothing. Each retained tool asks
    the live watch posture before execution. The caller saves and restores the
    original tool arrays before releasing the turn.
    """
    validate_watch_snapshot(snapshot)
    strict = read_report_policy(policy)
    servers = {
        s.name: s
        for s in snapshot.agent.mcp_servers
        if s.name not in (DAIMON_SERVER_NAME, "sandbox")
        and not (public_url and same_mcp_endpoint(s.url, public_url))
    }
    tools: list[dict[str, Any]] = []
    for tool in snapshot.agent.tools:
        if tool.type != "mcp_toolset" or tool.mcp_server_name not in servers:
            continue
        configs = [
            {"name": config.name, "enabled": True, "permission_policy": {"type": "always_ask"}}
            for config in tool.configs
            if config.enabled
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", config.name)
            and classify_tool(strict, server_name=tool.mcp_server_name, tool_name=config.name)
            == "read"
        ]
        if configs:
            tools.append(
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": tool.mcp_server_name,
                    "default_config": {
                        "enabled": False,
                        "permission_policy": {"type": "always_ask"},
                    },
                    "configs": configs,
                }
            )
    names = {tool["mcp_server_name"] for tool in tools}
    agent: dict[str, Any] = {
        "tools": tools,
        "mcp_servers": [servers[name].model_dump(mode="json") for name in servers if name in names],
    }
    await update_session_record(
        client,
        snapshot.id,
        scope=scope,
        agent_id=snapshot.agent.id,
        revision=Revision(local=snapshot.agent.version, native=str(snapshot.agent.version)),
        agent=cast(BetaManagedAgentsSessionAgentUpdateParam, agent),
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
