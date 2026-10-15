"""GitHub App grant checks and read-token minting for skill imports."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

import httpx
from daimon.core.github_app_auth import build_app_jwt, mint_installation_token
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

RepoAccessKind = Literal["connected", "checked", "needs_attention", "not_checked"]
RepoCredential = Literal["github_app", "none"]


class RepoAccess(BaseModel):
    """The access shown for an agent's working repository."""

    model_config = ConfigDict(frozen=True)
    kind: RepoAccessKind
    credential: RepoCredential
    checked_at: datetime | None = None
    checked_by_account_id: uuid.UUID | None = None
    corrective: str | None = None


def normalize_owner_repo(url: str) -> str:
    """Extract owner/repo from a GitHub URL or short name."""
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


async def mint_granted_skill_token(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    repo_url: str,
    app_id: str | None,
    app_private_key: SecretStr | None,
    now: int,
) -> str | None:
    """Mint a read token only for this agent's active App grant."""
    if agent_id is None or app_id is None or app_private_key is None:
        return None
    from daimon.core.stores.github_access import active_grant_for_repo

    grant = await active_grant_for_repo(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_name=normalize_owner_repo(repo_url),
    )
    if grant is None:
        return None
    jwt = build_app_jwt(app_private_key.get_secret_value(), app_id, now=now)
    return await mint_installation_token(
        http_client,
        jwt=jwt,
        installation_id=grant.installation_id,
        repository=grant.repo_full_name.split("/", 1)[1],
        permissions={"contents": "read"},
    )
