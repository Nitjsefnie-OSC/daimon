"""One-use chat grants awaiting a verified personal GitHub link."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from daimon.core._models import GitHubLinkedConnectPending
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# How long a claimed request stays out of reach of another `take`. A request not
# resolved by then (the process died mid-batch) can be taken again.
CLAIM_LEASE = timedelta(minutes=2)


class PendingGrant(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    ma_agent_id: str
    agent_name: str
    repo_name: str
    requested_access: Literal["read", "write"]
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
    requested_access: Literal["read", "write"] = "read",
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
            requested_access=requested_access,
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
    now: datetime | None = None,
) -> list[PendingGrant]:
    """Claim this requester's unexpired requests for `CLAIM_LEASE`, oldest first.

    A claimed request is not deleted: `resolve` deletes it once it has an outcome.
    Rows another `take` claimed and has not yet released are skipped until their
    lease passes. Expired requests are deleted.
    """
    now = now or datetime.now(UTC)
    rows = (
        await session.scalars(
            select(GitHubLinkedConnectPending)
            .where(
                GitHubLinkedConnectPending.tenant_id == tenant_id,
                GitHubLinkedConnectPending.account_id == account_id,
                GitHubLinkedConnectPending.platform == platform,
                GitHubLinkedConnectPending.platform_user_id == platform_user_id,
                GitHubLinkedConnectPending.expires_at > now,
                or_(
                    GitHubLinkedConnectPending.claimed_until.is_(None),
                    GitHubLinkedConnectPending.claimed_until <= now,
                ),
            )
            .order_by(GitHubLinkedConnectPending.expires_at, GitHubLinkedConnectPending.id)
            .with_for_update(skip_locked=True)
        )
    ).all()
    pending = [PendingGrant.model_validate(row) for row in rows]
    if rows:
        await session.execute(
            update(GitHubLinkedConnectPending)
            .where(GitHubLinkedConnectPending.id.in_([row.id for row in rows]))
            .values(claimed_until=now + CLAIM_LEASE)
        )
    await session.execute(
        delete(GitHubLinkedConnectPending).where(GitHubLinkedConnectPending.expires_at <= now)
    )
    return pending


async def resolve(session: AsyncSession, request_id: uuid.UUID) -> None:
    """Delete one request once it has an outcome and its result was delivered."""
    await session.execute(
        delete(GitHubLinkedConnectPending).where(GitHubLinkedConnectPending.id == request_id)
    )
