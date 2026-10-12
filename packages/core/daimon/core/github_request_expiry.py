"""Settle requests nobody answered in time.

Two kinds of request share this poller: a GitHub access wait gets one final
line after seven days, and an unclicked private form (a credential request)
has its card re-rendered as expired once its deadline passes, so the channel
never shows a live button for a form that can no longer open.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import structlog
from daimon.core.stores.credential_requests import expire_unclicked_credential_requests
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.stores.github_access_requests import (
    AccessRequest,
    claim_due_expiry_group,
    mark_expiry_notice_sent,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PostExpiry = Callable[[AccessRequest], Awaitable[bool]]
RetireForm = Callable[[CredentialRequestRow], Awaitable[None]]
_log = structlog.get_logger(__name__)


async def poll_expired_requests_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    post: PostExpiry,
    now: datetime | None = None,
    limit: int = 20,
) -> int:
    """Claim, post and settle groups while the row locks prevent duplicate posts."""
    current = now or datetime.now(UTC)
    delivered = 0
    for _ in range(limit):
        async with sessionmaker.begin() as session:
            group = await claim_due_expiry_group(session, platform=platform, now=current)
            if not group:
                break
            if not await post(group[0]):
                break
            await mark_expiry_notice_sent(
                session, request_ids=tuple(row.id for row in group), now=current
            )
            delivered += 1
    return delivered


async def retire_expired_forms_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    retire: RetireForm,
    now: datetime | None = None,
) -> int:
    """Spend this platform's unclicked, past-deadline forms, then retire each card.

    The rows commit as spent before any card edit: `retire` is a best-effort
    re-render (a deleted message or archived thread must not bring the row
    back every tick), and a late click still gets the expired reply from the
    row itself.
    """
    async with sessionmaker.begin() as session:
        rows = await expire_unclicked_credential_requests(
            session, platform=platform, now=now or datetime.now(UTC)
        )
    for row in rows:
        await retire(row)
    return len(rows)


async def run_request_expiry_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: Literal["discord", "slack"],
    post: PostExpiry,
    should_stop: Callable[[], bool],
    retire_form: RetireForm | None = None,
    interval_s: float = 60.0,
) -> None:
    while not should_stop():
        try:
            await poll_expired_requests_once(sessionmaker, platform=platform, post=post)
        except Exception:
            _log.exception("github_request.expiry_poll_failed", platform=platform)
        if retire_form is not None:
            try:
                await retire_expired_forms_once(sessionmaker, platform=platform, retire=retire_form)
            except Exception:
                _log.exception("credential_request.expiry_poll_failed", platform=platform)
        await asyncio.sleep(interval_s)
