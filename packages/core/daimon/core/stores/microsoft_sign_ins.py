"""Async store for microsoft_sign_ins: the single-use Connect Microsoft links.

`create_sign_in` replaces the person's earlier links, so only the newest card
works. `consume_sign_in` deletes the row it returns: one statement, so a
replayed or raced callback finds nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import MicrosoftSignIn
from daimon.core.stores.domain import MicrosoftSignInRow
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


async def create_sign_in(
    session: AsyncSession,
    *,
    state: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    user_id: str,
    code_verifier: str,
    expires_at: datetime,
    now: datetime,
) -> MicrosoftSignInRow:
    await session.execute(
        delete(MicrosoftSignIn).where(
            MicrosoftSignIn.tenant_id == tenant_id, MicrosoftSignIn.user_id == user_id
        )
    )
    orm = MicrosoftSignIn(
        state=state,
        tenant_id=tenant_id,
        account_id=account_id,
        user_id=user_id,
        code_verifier=code_verifier,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(orm)
    await session.flush()
    return MicrosoftSignInRow.model_validate(orm)


async def get_live_sign_in(
    session: AsyncSession, *, state: str, now: datetime
) -> MicrosoftSignInRow | None:
    orm = await session.scalar(
        select(MicrosoftSignIn).where(
            MicrosoftSignIn.state == state, MicrosoftSignIn.expires_at > now
        )
    )
    return None if orm is None else MicrosoftSignInRow.model_validate(orm)


async def consume_sign_in(
    session: AsyncSession, *, state: str, now: datetime
) -> MicrosoftSignInRow | None:
    """Spend `state` iff it is unexpired; None when unknown, spent or expired."""
    stmt = (
        delete(MicrosoftSignIn)
        .where(MicrosoftSignIn.state == state, MicrosoftSignIn.expires_at > now)
        .returning(MicrosoftSignIn)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else MicrosoftSignInRow.model_validate(orm)


async def delete_sign_in(session: AsyncSession, *, state: str) -> None:
    """Withdraw a link whose card never reached the person."""
    await session.execute(delete(MicrosoftSignIn).where(MicrosoftSignIn.state == state))
