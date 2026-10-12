"""The channel promo code migration round-trips and drops only channel codes."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.promo_codes import build_promo_code_terms
from daimon.core.stores import promo_codes as promo_store
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0088_channel_promo_codes.py"
    spec = importlib.util.spec_from_file_location("migration_channel_promo_codes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_downgrade_drops_channel_codes_and_upgrade_admits_them_again(
    db_session: AsyncSession,
) -> None:
    """Downgrade keeps server-wide codes and their one-per-tenant rule; upgrade restores channels."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    server = await promo_store.insert_promo_code(
        db_session,
        code_hash="server",
        terms=build_promo_code_terms(amount_usd=Decimal("3"), timed=False),
    )
    channel = await promo_store.insert_promo_code(
        db_session,
        code_hash="channel",
        terms=build_promo_code_terms(
            amount_usd=Decimal("3"), timed=False, channel_cap_usd=Decimal("6")
        ),
    )
    assert server is not None and channel is not None
    for code_id, channel_id in ((server.id, None), (channel.id, "c1")):
        assert await promo_store.insert_redemption(
            db_session,
            promo_code_id=code_id,
            tenant_id=tenant.id,
            account_id=account.id,
            now=server.created_at,
            granted=True,
            channel_id=channel_id,
        )
    migration = _migration()
    conn = await db_session.connection()

    def downgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.downgrade()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(downgrade)
    hashes = await db_session.execute(text("SELECT code_hash FROM promo_codes"))
    assert hashes.scalars().all() == ["server"], "only the channel code is dropped"
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO promo_redemptions (promo_code_id, tenant_id, redeemed_at)"
                    " VALUES (:c, :t, now())"
                ),
                {"c": server.id, "t": tenant.id},
            )

    await conn.run_sync(upgrade)
    again = await promo_store.insert_promo_code(
        db_session,
        code_hash="again",
        terms=build_promo_code_terms(
            amount_usd=Decimal("3"), timed=False, channel_cap_usd=Decimal("3")
        ),
    )
    assert again is not None and again.channel_cap_usd == Decimal("3"), "channel codes are back"
