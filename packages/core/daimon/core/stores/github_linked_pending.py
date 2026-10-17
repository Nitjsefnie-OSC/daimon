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
# resolved by then (the process died mid-batch) can be taken again. The resume
# renews the lease of every request it still holds before each request runs.
CLAIM_LEASE = timedelta(minutes=2)
# The longest one request may run, outcome and result delivery included. The
# resume enforces it with `asyncio.timeout`; a request that hits it is answered
# with a failure line and resolved, so the next request runs.
PER_REQUEST_DEADLINE = timedelta(minutes=5)
# The longest the failure line for a request that hit its deadline may take.
FAILURE_DELIVERY_DEADLINE = timedelta(seconds=30)
# A claimed row is garbage once its lease lapsed this long ago. A live callback
# renews every row it holds before each request, so the gap between two
# renewals is at most `PER_REQUEST_DEADLINE + FAILURE_DELIVERY_DEADLINE` plus
# database time, and the lease runs `CLAIM_LEASE` past each renewal. No live
# holder is an hour behind; the gap only clears rows a crashed holder left.
CLAIMED_GARBAGE_AFTER = timedelta(hours=1)
assert PER_REQUEST_DEADLINE + FAILURE_DELIVERY_DEADLINE + CLAIM_LEASE < CLAIMED_GARBAGE_AFTER, (
    "a live holder must renew well inside the garbage window"
)


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
    `renew_claim`, `renew` and `resolve` check, so a lapsed claim cannot act on a
    row someone else has since taken. Expired requests are deleted if never
    claimed, or once their lease lapsed `CLAIMED_GARBAGE_AFTER` ago.

    That grace is safe because a live holder cannot be that far behind. Before
    each request it calls `renew_claim`, which extends the lease of every row
    it still holds, and each request runs under `PER_REQUEST_DEADLINE`, so the
    gap between two renewals is at most `PER_REQUEST_DEADLINE` plus one failure
    delivery (`FAILURE_DELIVERY_DEADLINE`), under the hour (checked at import).
    A holder whose row another `take` claimed anyway finds `renew_claim` no
    longer returns it and skips that request.

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


async def renew_claim(
    session: AsyncSession, claim_token: uuid.UUID | None, *, now: datetime | None = None
) -> set[uuid.UUID]:
    """Extend the lease of every request this claim holds; return their ids.

    One statement, so a slow request cannot let the lease of the rows still
    waiting behind it lapse. A row missing from the result is gone or another
    claim owns it now, and the caller must not answer it.
    """
    if claim_token is None:
        return set()
    result = await session.execute(
        update(GitHubLinkedConnectPending)
        .where(GitHubLinkedConnectPending.claim_token == claim_token)
        .values(claimed_until=(now or datetime.now(UTC)) + CLAIM_LEASE)
        .returning(GitHubLinkedConnectPending.id)
    )
    return set(result.scalars())


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
