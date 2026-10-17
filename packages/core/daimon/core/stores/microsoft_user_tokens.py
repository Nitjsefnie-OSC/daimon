"""Async store for microsoft_user_tokens: one encrypted refresh token per person.

Ciphertext in and out; the Fernet key never reaches the store. Re-connecting
and every refresh replace the row's token; `updated_at` tells a reader whether
the token it cached an access token for is still the stored one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import MicrosoftUserToken
from daimon.core.stores.domain import MicrosoftUserTokenRow
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_microsoft_user_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    encrypted_refresh_token: bytes,
    scopes: str,
    now: datetime,
) -> MicrosoftUserTokenRow:
    stmt = (
        insert(MicrosoftUserToken)
        .values(
            tenant_id=tenant_id,
            user_id=user_id,
            encrypted_refresh_token=encrypted_refresh_token,
            scopes=scopes,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[MicrosoftUserToken.tenant_id, MicrosoftUserToken.user_id],
            set_={
                "encrypted_refresh_token": encrypted_refresh_token,
                "scopes": scopes,
                "updated_at": now,
            },
        )
        .returning(MicrosoftUserToken)
    )
    return MicrosoftUserTokenRow.model_validate((await session.execute(stmt)).scalar_one())


async def get_microsoft_user_token(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: str
) -> MicrosoftUserTokenRow | None:
    orm = await session.get(MicrosoftUserToken, (tenant_id, user_id))
    return None if orm is None else MicrosoftUserTokenRow.model_validate(orm)


async def update_microsoft_refresh_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    read_at: datetime,
    encrypted_refresh_token: bytes,
    scopes: str,
    now: datetime,
) -> MicrosoftUserTokenRow | None:
    """Store a rotated token, only over the row it was refreshed from.

    None when the row changed or went since `read_at` (a reconnect, a
    disconnect): the newer state wins and the rotated token is dropped.
    """
    stmt = (
        update(MicrosoftUserToken)
        .where(
            MicrosoftUserToken.tenant_id == tenant_id,
            MicrosoftUserToken.user_id == user_id,
            MicrosoftUserToken.updated_at == read_at,
        )
        .values(encrypted_refresh_token=encrypted_refresh_token, scopes=scopes, updated_at=now)
        .returning(MicrosoftUserToken)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else MicrosoftUserTokenRow.model_validate(orm)


async def delete_microsoft_user_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    read_at: datetime | None = None,
) -> int:
    """Delete one person's connection; with `read_at`, only if unchanged since. Idempotent."""
    stmt = delete(MicrosoftUserToken).where(
        MicrosoftUserToken.tenant_id == tenant_id, MicrosoftUserToken.user_id == user_id
    )
    if read_at is not None:
        stmt = stmt.where(MicrosoftUserToken.updated_at == read_at)
    result = await session.execute(stmt)
    await session.flush()
    return cast(CursorResult[Any], result).rowcount


async def count_microsoft_user_tokens(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: str
) -> int:
    """How many rows `delete_microsoft_user_token` would delete: 0 or 1."""
    stmt = (
        select(func.count())
        .select_from(MicrosoftUserToken)
        .where(MicrosoftUserToken.tenant_id == tenant_id, MicrosoftUserToken.user_id == user_id)
    )
    return int((await session.execute(stmt)).scalar_one())
