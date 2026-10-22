"""Whether the MCP caller is the recorded owner of an agent (`daimon.core.stores.agent_owners`).

Only a person's chat credential can be an owner: an agent-scoped key, a
client requester from another organisation and a credential with no person
behind it never are. The owner is read at each check, so a change of owner
applies to the next call.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_owners


def may_own(auth: AuthIdentity) -> bool:
    """A person's own credential, the only kind that can stand for an agent's owner."""
    return auth.agent_id is None and auth.platform_user_id is not None and not auth.client


async def caller_owns_agent(runtime: McpRuntime, auth: AuthIdentity, ma_agent_id: str) -> bool:
    if not may_own(auth):
        return False
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=ma_agent_id)
    async with runtime.session_factory() as session:
        owner = await agent_owners.owner_of(session, auth.tenant_id, agent_id)
    return owner is not None and owner == auth.account_id
