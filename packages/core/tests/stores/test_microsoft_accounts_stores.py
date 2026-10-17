"""Real-Postgres tests for the microsoft_user_tokens and microsoft_sign_ins stores.

Covers the single-use sign-in consume (the callback's replay gate), the
newest-link-wins replacement, and the compare-on-`updated_at` writes that keep
a stale refresh from overwriting a reconnect or resurrecting a disconnect.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores import microsoft_sign_ins as sign_ins
from daimon.core.stores import microsoft_user_tokens as tokens
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_OID = "00000000-0000-0000-0000-0000000000a1"


async def _token(session: AsyncSession, tenant_id: uuid.UUID, *, ct: bytes, now: datetime):
    return await tokens.upsert_microsoft_user_token(
        session,
        tenant_id=tenant_id,
        user_id=_OID,
        encrypted_refresh_token=ct,
        scopes="User.Read",
        now=now,
    )


async def test_token_upsert_replaces_on_reconnect_and_is_per_tenant(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    first = await _token(db_session, tenant.id, ct=b"old", now=_NOW)
    second = await _token(db_session, tenant.id, ct=b"new", now=_NOW + timedelta(minutes=1))
    assert second.encrypted_refresh_token == b"new", "reconnect must replace the ciphertext"
    assert second.created_at == first.created_at, "reconnect keeps the original created_at"
    assert second.updated_at > first.updated_at, "reconnect must bump updated_at"
    missing = await tokens.get_microsoft_user_token(db_session, tenant_id=other.id, user_id=_OID)
    assert missing is None, "the same oid in another tenant must not match"


async def test_rotated_token_only_lands_over_the_row_it_came_from(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    row = await _token(db_session, tenant.id, ct=b"rt-1", now=_NOW)
    later = _NOW + timedelta(minutes=1)
    stored = await tokens.update_microsoft_refresh_token(
        db_session,
        tenant_id=tenant.id,
        user_id=_OID,
        read_at=row.updated_at,
        encrypted_refresh_token=b"rt-2",
        scopes="User.Read",
        now=later,
    )
    assert stored is not None and stored.encrypted_refresh_token == b"rt-2", (
        "a rotation over the unchanged row must be stored"
    )
    stale = await tokens.update_microsoft_refresh_token(
        db_session,
        tenant_id=tenant.id,
        user_id=_OID,
        read_at=row.updated_at,
        encrypted_refresh_token=b"rt-stale",
        scopes="User.Read",
        now=later + timedelta(minutes=1),
    )
    assert stale is None, "a rotation from an older read must not overwrite the newer token"


async def test_conditional_delete_spares_a_reconnected_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    row = await _token(db_session, tenant.id, ct=b"old", now=_NOW)
    await _token(db_session, tenant.id, ct=b"new", now=_NOW + timedelta(minutes=1))
    deleted = await tokens.delete_microsoft_user_token(
        db_session, tenant_id=tenant.id, user_id=_OID, read_at=row.updated_at
    )
    assert deleted == 0, "an invalid_grant on the old token must not delete a fresh reconnect"
    count = await tokens.count_microsoft_user_tokens(db_session, tenant_id=tenant.id, user_id=_OID)
    assert count == 1, "the reconnected row must survive"
    assert (
        await tokens.delete_microsoft_user_token(db_session, tenant_id=tenant.id, user_id=_OID) == 1
    ), "an unconditional delete (disconnect) must remove the row"
    assert (
        await tokens.delete_microsoft_user_token(db_session, tenant_id=tenant.id, user_id=_OID) == 0
    ), "a second delete must be a no-op"


async def _sign_in(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID, *, expires_at: datetime
):
    return await sign_ins.create_sign_in(
        session,
        state="st_" + uuid.uuid4().hex,
        tenant_id=tenant_id,
        account_id=account_id,
        user_id=_OID,
        code_verifier="verifier",
        expires_at=expires_at,
        now=_NOW,
    )


async def test_sign_in_consumes_once(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await _sign_in(db_session, tenant.id, account.id, expires_at=_NOW + timedelta(minutes=10))
    live = await sign_ins.get_live_sign_in(db_session, state=row.state, now=_NOW)
    assert live == row, "the start route must find the live link"
    assert await sign_ins.consume_sign_in(db_session, state=row.state, now=_NOW) == row, (
        "the first callback must spend the state and get the row back"
    )
    assert await sign_ins.consume_sign_in(db_session, state=row.state, now=_NOW) is None, (
        "a replayed callback must find nothing"
    )


async def test_expired_sign_in_is_neither_live_nor_consumable(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await _sign_in(db_session, tenant.id, account.id, expires_at=_NOW)
    assert await sign_ins.get_live_sign_in(db_session, state=row.state, now=_NOW) is None, (
        "an expired link must not start a sign-in"
    )
    assert await sign_ins.consume_sign_in(db_session, state=row.state, now=_NOW) is None, (
        "an expired state must not complete a sign-in"
    )


async def test_new_sign_in_replaces_the_persons_older_link(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    old = await _sign_in(db_session, tenant.id, account.id, expires_at=_NOW + timedelta(minutes=10))
    new = await _sign_in(db_session, tenant.id, account.id, expires_at=_NOW + timedelta(minutes=10))
    assert await sign_ins.get_live_sign_in(db_session, state=old.state, now=_NOW) is None, (
        "an older card's link must stop working once a newer one is minted"
    )
    assert await sign_ins.get_live_sign_in(db_session, state=new.state, now=_NOW) == new, (
        "the newest link must stay live"
    )
