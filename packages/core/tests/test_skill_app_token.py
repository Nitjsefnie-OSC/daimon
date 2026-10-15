"""Private skill imports require an active grant on the target agent."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.core import github_repo_auth
from daimon.core._models import Tenant, TenantGitHubRepo
from daimon.core.stores import github_access, github_app_installations
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_skill_token_requires_this_agents_active_grant(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    await github_app_installations.upsert(
        db_session, installation_id=7, account_login="team", repo_full_names=["team/private"]
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=101,
            owner_id=1,
            installation_id=7,
            repo_full_name="team/private",
            max_access="read",
            authorized_by_github_user_id=2,
            status="active",
        )
    )
    await db_session.flush()
    await github_access.stage_grant(
        db_session,
        tenant_id=tenant_id,
        agent_id=other_id,
        repo_id=101,
        baseline_access="read",
        ceiling_access="read",
        granted_by_account_id=None,
    )
    mint = AsyncMock(return_value="installation-token")

    def fake_app_jwt(_key: str, _app_id: str, *, now: int | None = None) -> str:
        return "jwt"

    monkeypatch.setattr(github_repo_auth, "build_app_jwt", fake_app_jwt)
    monkeypatch.setattr(github_repo_auth, "mint_installation_token", mint)
    async with httpx.AsyncClient() as http:
        assert (
            await github_repo_auth.mint_granted_skill_token(
                db_session,
                http,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_url="https://github.com/team/private.git",
                app_id="123",
                app_private_key=SecretStr("pem"),
                now=1,
            )
            is None
        )
        mint.assert_not_awaited()
        await github_access.stage_grant(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=101,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=None,
        )
        assert (
            await github_repo_auth.mint_granted_skill_token(
                db_session,
                http,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_url="https://github.com/team/private.git",
                app_id="123",
                app_private_key=SecretStr("pem"),
                now=1,
            )
            == "installation-token"
        )
        mint.assert_awaited_once()
