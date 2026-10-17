"""One-use chat grants awaiting a verified personal GitHub link."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from daimon.core._models import GitHubLinkedConnectPending
from pydantic import BaseModel, ConfigDict
from sqlalchemy import CursorResult, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# How long a claimed request stays out of reach of another `take`. A request not
# resolved by then (the process died mid-batch) can be taken again. `renew` gives
# each request a fresh lease just before it runs.
CLAIM_LEASE = timedelta(minutes=2)
# A claimed row is garbage once its lease lapsed this long ago. A live callback
# renews before each request and every GitHub call has a timeout, so no live
# holder is this far behind; the gap only clears rows a crashed holder left.
CLAIMED_GARBAGE_AFTER = timedelta(hours=1)


class ClaimLostError(Exception):
    """Another `take` owns this request now; the caller must not answer it."""


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
    # Set by `take`: only the claim that holds this token may renew or resolve the row.
    claim_token: uuid.UUID | None = None


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
    lease passes. Each returned request carries this claim's token, which
    `renew` and `resolve` check, so a lapsed claim cannot act on a row someone
    else has since taken. Expired requests are deleted if never claimed, or once
    their lease lapsed `CLAIMED_GARBAGE_AFTER` ago, so a slow holder whose
    lease lapsed mid-batch still finds its row and answers it.

    Stated limit: the resume runs inline in the OAuth callback request, so a
    crash shows the person a failed browser request and nothing retries it. Its
    requests become takeable again once the lease lapses, by the person's next
    link callback, until they expire; no scheduler recovers them.
    """
    now = now or datetime.now(UTC)
    token = uuid.uuid4()
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
    pending = [
        PendingGrant.model_validate(row).model_copy(update={"claim_token": token}) for row in rows
    ]
    if rows:
        await session.execute(
            update(GitHubLinkedConnectPending)
            .where(GitHubLinkedConnectPending.id.in_([row.id for row in rows]))
            .values(claimed_until=now + CLAIM_LEASE, claim_token=token)
            .execution_options(synchronize_session=False)
        )
    await session.execute(
        delete(GitHubLinkedConnectPending).where(
            GitHubLinkedConnectPending.expires_at <= now,
            or_(
                GitHubLinkedConnectPending.claimed_until.is_(None),
                GitHubLinkedConnectPending.claimed_until <= now - CLAIMED_GARBAGE_AFTER,
            ),
        )
    )
    return pending


async def renew(
    session: AsyncSession,
    request_id: uuid.UUID,
    claim_token: uuid.UUID | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Extend this claim's lease on one request; False when another claim owns it now."""
    if claim_token is None:
        return False
    result = await session.execute(
        update(GitHubLinkedConnectPending)
        .where(
            GitHubLinkedConnectPending.id == request_id,
            GitHubLinkedConnectPending.claim_token == claim_token,
        )
        .values(claimed_until=(now or datetime.now(UTC)) + CLAIM_LEASE)
    )
    return cast(CursorResult[Any], result).rowcount > 0


async def resolve(
    session: AsyncSession, request_id: uuid.UUID, claim_token: uuid.UUID | None
) -> bool:
    """Delete one request once it has an outcome, only while this claim still owns it.

    True when the row was deleted. False means it is gone or another claim took
    it, and this claim leaves it alone.
    """
    if claim_token is None:
        return False
    result = await session.execute(
        delete(GitHubLinkedConnectPending).where(
            GitHubLinkedConnectPending.id == request_id,
            GitHubLinkedConnectPending.claim_token == claim_token,
        )
    )
    return cast(CursorResult[Any], result).rowcount > 0
