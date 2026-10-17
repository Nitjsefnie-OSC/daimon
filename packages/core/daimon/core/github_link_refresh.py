"""Keep linked GitHub user tokens alive so a person links once.

The personal token is refreshed whenever Connect uses it, but someone who stops
adding repositories would reach the six-month refresh-token expiry and have to
link again. This sweep spends each refresh token that is close to expiry through
the same serialized refresh path Connect uses, which gives the row a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import structlog
from cryptography.fernet import MultiFernet
from daimon.core.github_requester_access import linked_access_token_with_generation
from daimon.core.stores.github_links import list_users_with_expiring_refresh
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LinkRefreshCounts:
    refreshed: int = 0
    broken: int = 0
    failed: int = 0


async def refresh_expiring_links(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
    now: datetime,
    window: timedelta = timedelta(days=30),
    limit: int = 50,
) -> LinkRefreshCounts:
    """Refresh up to ``limit`` active links whose refresh token expires within ``window``.

    A link whose refresh token GitHub rejects becomes broken, as on any Connect
    use. A transport or 429/5xx failure leaves the row for the next sweep.
    Failures are logged by GitHub user id only.
    """
    before = now + window
    async with sessionmaker() as session:
        user_ids = await list_users_with_expiring_refresh(
            session, now=now, before=before, limit=limit
        )
    refreshed = broken = failed = 0
    for user_id in user_ids:
        try:
            token, _ = await linked_access_token_with_generation(
                sessionmaker,
                client,
                user_id=user_id,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
                refresh_if_refresh_expires_before=before,
            )
        except Exception as exc:
            failed += 1
            log.warning(
                "github_link_refresh.failed",
                github_user_id=user_id,
                error_type=type(exc).__name__,
                status_code=exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None,
            )
            continue
        if token is None:
            broken += 1
            log.info("github_link_refresh.broken", github_user_id=user_id)
        else:
            refreshed += 1
    return LinkRefreshCounts(refreshed=refreshed, broken=broken, failed=failed)
