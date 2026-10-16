"""Experimental, agent-scoped grants from a verified personal GitHub link."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from daimon.adapters.mcp.oauth_github import _installations
from daimon.core._models import (
    Account,
    AccountGitHubLink,
    GitHubAppInstallation,
    GitHubUserLink,
    TenantGitHubRepo,
)
from daimon.core.github_app_auth import (
    build_app_jwt,
    get_app_installation_details,
    installation_supports_write,
)
from daimon.core.github_requester_access import linked_access_token
from daimon.core.stores.github_access import list_agent_grants, stage_grant
from daimon.core.stores.github_app_installations import upsert_github_app
from daimon.core.stores.github_links import get_account_link, get_user
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LinkedStatus = Literal[
    "granted",
    "granted_read_only",
    "needs_link",
    "relink",
    "no_github_access",
    "needs_install",
    "install_pending",
    "changed_try_again",
]


class LinkedOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: LinkedStatus
    message: str


def proposed_linked_grant_policy(permissions: dict[str, bool]) -> tuple[str, str] | None:
    """PROPOSED: push/maintain/admin grants write; triage/pull grants read."""
    for key in ("admin", "maintain", "push", "triage", "pull"):
        if permissions.get(key) is True:
            return ("write" if key in ("admin", "maintain", "push") else "read", key)
    return None


async def _before_commit() -> None:
    """Test seam for revocation between GitHub's answer and the DB transaction."""


async def linked_grant(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
    repo_name: str,
    fernet: object,
    client_id: str,
    client_secret: str,
    app_id: str,
    private_key: str,
    manages_now: Callable[[AsyncSession], Awaitable[bool]],
) -> LinkedOutcome:
    async with sessionmaker() as session:
        account = await session.get(Account, account_id)
        if account is None or account.tenant_id != tenant_id or account.is_external:
            return LinkedOutcome(
                status="needs_link", message="Link GitHub to check your repo access."
            )
        link = await get_account_link(session, account_id=account_id)
        if link is None:
            return LinkedOutcome(
                status="needs_link", message="Link GitHub to check your repo access."
            )
        user = await get_user(session, github_user_id=link.github_user_id)
        if user is None or user.status != "active":
            return LinkedOutcome(status="relink", message="Your GitHub link needs renewing.")
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            access_token = await linked_access_token(
                sessionmaker,
                client,
                user_id=link.github_user_id,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
            )
            if access_token is None:
                return LinkedOutcome(status="relink", message="Your GitHub link needs renewing.")
            installations = await _installations(client, access_token)
            owner = repo_name.split("/", 1)[0]
            candidates = [i for i in installations if i.owner_login.casefold() == owner.casefold()]
            covering = [
                (i, r)
                for i in candidates
                for r in i.repos
                if r.full_name.casefold() == repo_name.casefold()
            ]
            if not covering:
                message = (
                    "Add this repo on GitHub's App installation page."
                    if any(i.repository_selection == "selected" for i in candidates)
                    else "Install the GitHub App for this owner first."
                )
                return LinkedOutcome(status="needs_install", message=message)
            installation, listed_repo = covering[0]
            if not listed_repo.usable:
                return LinkedOutcome(
                    status="no_github_access",
                    message="Your GitHub account cannot access this repo.",
                )
            detail = await get_app_installation_details(
                client,
                jwt=build_app_jwt(private_key, app_id, now=int(time.time())),
                installation_id=installation.id,
            )
            if (
                detail.account_id != installation.owner_id
                or detail.account_login.casefold() != installation.owner_login.casefold()
                or detail.suspended_at is not None
                or not installation_supports_write(detail.permissions)
            ):
                return LinkedOutcome(
                    status="needs_install",
                    message="The GitHub App installation cannot cover this repo now.",
                )
            async with sessionmaker() as session:
                fresh_user = await get_user(session, github_user_id=link.github_user_id)
            if fresh_user is None or fresh_user.status != "active":
                return LinkedOutcome(status="relink", message="Your GitHub link needs renewing.")
            response = await client.get(
                f"https://api.github.com/repos/{repo_name}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if response.status_code == 401:
                return LinkedOutcome(status="relink", message="Your GitHub link needs renewing.")
            if response.status_code == 404:
                return LinkedOutcome(
                    status="no_github_access",
                    message="Your GitHub account cannot access this repo.",
                )
            response.raise_for_status()
            repo = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return LinkedOutcome(status="relink", message="Your GitHub link needs renewing.")
            if exc.response.status_code == 403 and (
                exc.response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in exc.response.headers
            ):
                raise
            if exc.response.status_code in (403, 404):
                return LinkedOutcome(
                    status="no_github_access",
                    message="Your GitHub account cannot access this repo.",
                )
            raise
    if not isinstance(repo, dict) or repo.get("full_name", "").casefold() != repo_name.casefold():
        return LinkedOutcome(
            status="no_github_access", message="Your GitHub account cannot access this repo."
        )
    repo_id = repo.get("id")
    owner_data = repo.get("owner")
    owner_id = owner_data.get("id") if isinstance(owner_data, dict) else None
    if (
        not isinstance(repo_id, int)
        or not isinstance(owner_id, int)
        or repo_id != listed_repo.id
        or owner_id != installation.owner_id
    ):
        return LinkedOutcome(
            status="no_github_access", message="Your GitHub account cannot access this repo."
        )
    raw_permissions = repo.get("permissions")
    policy = proposed_linked_grant_policy(
        raw_permissions if isinstance(raw_permissions, dict) else {}
    )
    if policy is None:
        return LinkedOutcome(
            status="no_github_access", message="Your GitHub account cannot access this repo."
        )
    access, permission = policy
    if listed_repo.permission in ("triage", "read"):
        access = "read"
    checked_generation = fresh_user.link_generation
    async with sessionmaker() as session:
        original_installation = await session.get(GitHubAppInstallation, installation.id)
        originally_present = original_installation is not None
        original_names = (
            set(original_installation.repo_full_names) if original_installation else set()
        )
        if originally_present and original_installation.suspended_at is not None:
            return LinkedOutcome(status="changed_try_again", message="Access changed. Try again.")
    await _before_commit()

    async with sessionmaker.begin() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        current_link = await session.get(AccountGitHubLink, account_id, with_for_update=True)
        current_user = await session.get(GitHubUserLink, link.github_user_id, with_for_update=True)
        current_installation = await session.get(
            GitHubAppInstallation, installation.id, with_for_update=True
        )
        if (
            current_link is None
            or current_link.github_user_id != link.github_user_id
            or current_user is None
            or current_user.status != "active"
            or current_user.link_generation != checked_generation
            or (
                current_installation is not None
                and (
                    not originally_present
                    or current_installation.suspended_at is not None
                    or set(current_installation.repo_full_names) != original_names
                )
            )
            or (originally_present and current_installation is None)
            or not await manages_now(session)
        ):
            return LinkedOutcome(status="changed_try_again", message="Access changed. Try again.")
        current = await session.scalar(
            select(TenantGitHubRepo)
            .where(
                TenantGitHubRepo.tenant_id == tenant_id,
                TenantGitHubRepo.repo_id == repo_id,
                TenantGitHubRepo.scope_agent_id == agent_id,
            )
            .with_for_update()
        )
        if current is None:
            current = TenantGitHubRepo(
                tenant_id=tenant_id, repo_id=repo_id, scope_agent_id=agent_id
            )
            session.add(current)
        elif current.status != "active":
            current.version += 1
        current.owner_id = owner_id
        current.installation_id = installation.id
        current.repo_full_name = repo["full_name"]
        current.max_access = access
        current.authorized_by_github_user_id = link.github_user_id
        current.authorized_by_account_id = account_id
        current.authorized_permission = permission
        current.status = "active"
        current.status_reason = None
        await session.flush()
        await upsert_github_app(
            session,
            installation_id=installation.id,
            account_id=installation.owner_id,
            account_login=installation.owner_login,
            account_type=installation.owner_type,
            repository_selection=installation.repository_selection,
            suspended_at=None,
        )
        existing = next(
            (
                g
                for g in await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
                if g.repo_id == repo_id
            ),
            None,
        )
        await stage_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            baseline_access=access,
            ceiling_access=access,
            granted_by_account_id=account_id,
            mount_path=existing.mount_path if existing else None,
            is_working_repo=existing.is_working_repo if existing else False,
            agent_name=agent_name,
        )
    ability = "read and change" if access == "write" else "read"
    return LinkedOutcome(
        status="granted" if access == "write" else "granted_read_only",
        message=f"{agent_name} can now {ability} {repo_name}.",
    )
