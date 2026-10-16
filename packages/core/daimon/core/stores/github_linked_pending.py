"""One-use chat grants awaiting a verified personal GitHub link."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import GitHubLinkedConnectPending
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


class PendingGrant(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    ma_agent_id: str
    agent_name: str
    repo_name: str
    platform: str
    platform_user_id: str
    workspace_id: str
    parent_channel_id: str
    thread_id: str
    expires_at: datetime


async def save(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    ma_agent_id: str,
    agent_name: str,
    repo_name: str,
    platform: str,
    platform_user_id: str,
    workspace_id: str,
    parent_channel_id: str,
    thread_id: str,
) -> None:
    session.add(
        GitHubLinkedConnectPending(
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            ma_agent_id=ma_agent_id,
            agent_name=agent_name,
            repo_name=repo_name,
            platform=platform,
            platform_user_id=platform_user_id,
            workspace_id=workspace_id,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await session.flush()


async def take(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> list[PendingGrant]:
    rows = (
        await session.scalars(
            select(GitHubLinkedConnectPending)
            .where(
                GitHubLinkedConnectPending.tenant_id == tenant_id,
                GitHubLinkedConnectPending.account_id == account_id,
                GitHubLinkedConnectPending.platform == platform,
                GitHubLinkedConnectPending.platform_user_id == platform_user_id,
                GitHubLinkedConnectPending.expires_at > datetime.now(UTC),
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    pending = [PendingGrant.model_validate(row) for row in rows]
    for row in rows:
        await session.delete(row)
    await session.execute(
        delete(GitHubLinkedConnectPending).where(
            GitHubLinkedConnectPending.expires_at <= datetime.now(UTC)
        )
    )
    return pending
