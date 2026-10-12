"""Two tenants whose ids share their first 8 hex chars never reach each other's skills.

Every tenant's skills live in one MA workspace and a skill's title is the only
thing naming its tenant, so the title carries the whole tenant id.
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import _update_agent_impl
from daimon.adapters.mcp.tools.skills import _delete_impl, _get_impl, _list_impl
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.scope import DeploymentDefault
from daimon.testing import ma_agent
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TWIN_A = uuid.UUID("5ca1ab1e-0000-4000-8000-00000000000a")
TWIN_B = uuid.UUID("5ca1ab1e-ffff-4fff-8fff-00000000000b")
ACCOUNT = uuid.uuid4()


def _skill(skill_id: str, title: str) -> dict[str, Any]:
    return {
        "id": skill_id,
        "display_title": title,
        "source": "custom",
        "type": "skill",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "latest_version": "1",
    }


B_TITLE = tenant_scoped_display_title(tenant_id=TWIN_B, name="notes")
WORKSPACE = [_skill("sk_b", B_TITLE)]


def _client(deleted: list[str] | None = None, updated: list[str] | None = None) -> AsyncAnthropic:
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda _req, _m: list_response(WORKSPACE))
    agent = ma_agent(
        id="ag_a",
        name="a",
        metadata={"daimon_tenant": str(TWIN_A), "daimon_name": "a", "daimon_account": str(ACCOUNT)},
    )
    router.add(
        "GET", r"/v1/agents", lambda _req, _m: list_response([agent.model_dump(mode="json")])
    )
    router.add(
        "GET", r"/v1/agents/ag_a", lambda _req, _m: httpx.Response(200, json=agent.model_dump())
    )

    def on_update(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        if updated is not None:
            updated.append("ag_a")
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    def on_delete(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        if deleted is not None:
            deleted.append(m.group(1))
        return httpx.Response(200)

    router.add("POST", r"/v1/agents/ag_a", on_update)
    router.add("DELETE", r"/v1/skills/([^/]+)", on_delete)
    return build_fake_anthropic(router.dispatch)


def _runtime(client: AsyncAnthropic, factory: async_sessionmaker[AsyncSession]) -> McpRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    return McpRuntime(
        session_factory=factory,
        client=client,
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


def _auth() -> AuthIdentity:
    return AuthIdentity(account_id=ACCOUNT, tenant_id=TWIN_A, role=Role.ADMIN, is_admin=True)


async def test_list_hides_the_twins_skills(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await _list_impl(_runtime(_client(), db_session_factory), _auth())
    names = {info.name for info in result}
    assert "notes" not in names, "twin B's skill must not list for twin A"
    assert not any(n.startswith("5ca1ab1e") for n in names)


async def test_get_cannot_reach_the_twins_skill(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ToolError, match="not found"):
        await _get_impl(_runtime(_client(), db_session_factory), _auth(), "notes")


async def test_delete_cannot_reach_the_twins_skill(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deleted: list[str] = []
    with pytest.raises(ToolError, match="not found"):
        await _delete_impl(_runtime(_client(deleted=deleted), db_session_factory), _auth(), "notes")
    assert deleted == [], "nothing of twin B's is deleted"


async def test_attach_cannot_reach_the_twins_skill(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    updated: list[str] = []
    with pytest.raises(ToolError) as exc_info:
        await _update_agent_impl(
            _runtime(_client(updated=updated), db_session_factory),
            _auth(),
            name="a",
            model=None,
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=["notes"],
        )
    assert "not found" in str(exc_info.value)
    available = str(exc_info.value).split("available:")[-1]
    assert "notes" not in available, "the twin's skills are not offered as available either"
    assert updated == [], "the agent is never updated with the twin's skill"
