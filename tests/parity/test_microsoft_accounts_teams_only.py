"""Executable record: per-person Microsoft 365 access is Teams-only.

A Teams caller is an Entra user, so the account they sign in with can be
checked against them (tenant and object id). Discord and Slack callers have
no Entra identity to bind, so the `microsoft_*` tools carry only the `teams`
tag and refuse any other caller; Slack's counterpart is per-user Slack access
(`docs/slack.md`). If another platform gains a verifiable Entra identity,
update this record rather than deleting it.

No platform parametrization of scenarios, no database -- this is a scope check.
"""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.microsoft._person import require_person
from daimon.adapters.mcp.tools.microsoft.register import register_microsoft_tools
from daimon.core.stores.domain import Role
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

TOOLS = {
    "connect_microsoft",
    "microsoft_search",
    "microsoft_get",
    "microsoft_write",
    "microsoft_send",
}
ENTRA = "00000000-0000-4000-8000-0000000000aa"
OID = "00000000-0000-4000-8000-0000000000bb"


async def test_every_microsoft_tool_is_tagged_teams_only() -> None:
    mcp = FastMCP("microsoft")
    register_microsoft_tools(mcp, cast(McpRuntime, MagicMock()))
    tags = {tool.name: tool.tags for tool in await mcp.list_tools()}
    assert set(tags) == TOOLS, f"the Microsoft tool set changed: {sorted(tags)}"
    assert all(t == {"teams"} for t in tags.values()), (
        f"Microsoft tools must stay Teams-only until another platform can bind Entra: {tags}"
    )


@pytest.mark.parametrize("platform", ["discord", "slack"])
def test_other_platforms_are_refused_even_with_teams_like_ids(
    platform: Literal["discord", "slack"],
) -> None:
    graph = SimpleNamespace(config=SimpleNamespace(tenant_id=ENTRA))
    runtime = cast(McpRuntime, SimpleNamespace(microsoft=graph))
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        external_id=ENTRA,
        platform_user_id=OID,
    )
    with pytest.raises(ToolError, match="Teams conversation"):
        require_person(runtime, auth)
    teams = dataclasses.replace(auth, platform="teams")
    assert require_person(runtime, teams)[1] == OID, (
        "the same ids from Teams must pass, or the refusal above proves nothing"
    )
