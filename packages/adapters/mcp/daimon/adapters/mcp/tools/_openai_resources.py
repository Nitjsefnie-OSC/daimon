"""OpenAI agents for the MCP setup tools, behind the operator's resource-host capability.

A tool asks for a host only when the deployment opted in (`McpRuntime.resource_hosts`),
the turn's channel is freshly configured for the OpenAI profile, and the operator's
deployment manifest authorizes exactly that channel revision for the calling account
(`daimon.core.turn.openai_deployment.authorized_deployments`). Any other channel keeps
the existing Anthropic path; one on another non-Anthropic backend is refused. The
returned host must be bound to exactly that selection before it is used.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.channel_backend import channel_ref, current_backend
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError

if TYPE_CHECKING:
    from anthropic.types.beta import BetaManagedAgentsAgent
    from daimon.core.neutral_resources import NeutralResourceHost
    from mux.contracts.resources import Agent

OPENAI_PROFILE = "openai.persistent_workspace"
ANTHROPIC_PROFILE = "anthropic.managed_agents"
MCP_AUTHORIZATION_ID = "mcp-deployment-operator"


def _principal(auth: AuthIdentity) -> str:
    return f"mcp:{auth.account_id}"


async def openai_skill_host(
    runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow | None, *, operation: str
) -> NeutralResourceHost | None:
    """The bound OpenAI host for this turn's channel, or None for the existing path."""
    factory = runtime.resource_hosts
    if factory is None or origin is None:
        return None
    async with runtime.session_factory() as session:
        revision = await current_backend(
            session, channel_ref(auth.tenant_id, origin.platform, origin.parent_channel_id)
        )
    if revision is None or revision.profile == ANTHROPIC_PROFILE:
        return None
    if revision.profile != OPENAI_PROFILE:
        raise ToolError(
            "This channel's backend can't change an agent's skills from chat yet. "
            "Nothing was changed. Do not retry."
        )
    from daimon.core.turn.openai_deployment import authorized_deployments

    authorized = [
        deployment
        for deployment in authorized_deployments(str(auth.tenant_id))
        if deployment.platform == origin.platform
        and deployment.channel_id == origin.parent_channel_id
        and deployment.config_digest == revision.digest
        and deployment.account_id == str(auth.account_id)
        and deployment.project == factory.account_scope_id
    ]
    if len(authorized) != 1:
        return None
    return bind_host(
        factory(
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            principal_id=_principal(auth),
            authorization_id=MCP_AUTHORIZATION_ID,
            operation_key=f"mcp-{operation}:{uuid.uuid4()}",
        ),
        auth=auth,
        workspace=authorized[0].project,
    )


def bind_host(host: object, *, auth: AuthIdentity, workspace: str) -> NeutralResourceHost:
    """Refuse a host for any tenant, account, principal, authorization or workspace but ours."""
    from daimon.core.neutral_resources import NeutralResourceHost

    if not isinstance(host, NeutralResourceHost):
        raise ToolError("This deployment's resource host is misconfigured. Nothing was changed.")
    scope = host.scope
    capabilities = host.backend.capabilities()
    if (
        scope.tenant_id != str(auth.tenant_id)
        or scope.account_id != str(auth.account_id)
        or scope.principal_id != _principal(auth)
        or scope.authorization_id != MCP_AUTHORIZATION_ID
        or scope.is_platform
        or scope.is_legacy_host_authorized
        or host.workspace != workspace
        or capabilities.provider != "openai"
        or capabilities.profile_id != OPENAI_PROFILE
    ):
        raise ToolError(
            "This deployment's resource host is for a different tenant, account or workspace. "
            "Nothing was changed. Do not retry."
        )
    return host


@dataclass(frozen=True)
class _GateAgent:
    """The facts the shared skill and pin gates read, from a neutral agent."""

    id: str
    name: str
    metadata: Mapping[str, str]


def gate_view(agent: Agent) -> BetaManagedAgentsAgent:
    """A neutral agent as the shared gates read it: its native id, Daimon name and tags.

    The gates read only `id`, `name` and `metadata`; the same policy decides for
    both backends.
    """
    metadata = dict(agent.spec.metadata or {})
    return cast(
        "BetaManagedAgentsAgent",
        _GateAgent(
            id=agent.ref.id,
            name=metadata.get(MA_METADATA_KEY_NAME, agent.spec.name),
            metadata=metadata,
        ),
    )
