"""The channel promo code migration is expand-only and round-trips."""

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.promo_codes import build_promo_code_terms
from daimon.core.stores import promo_codes as promo_store
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

# The previous release's server-wide insert, verbatim: workers still running it
# during a rollout, or after an application-only rollback, must keep working.
OLD_SERVER_WIDE_INSERT = text(
    "INSERT INTO promo_redemptions"
    " (promo_code_id, tenant_id, redeemed_by_account_id, redeemed_at, granted_at)"
    " VALUES (:code, :tenant, :account, now(), now())"
    " ON CONFLICT (promo_code_id, tenant_id) DO NOTHING"
)
OLD_FAILURE_INSERT = text(
    "INSERT INTO promo_redeem_failures (tenant_id, attempted_at) VALUES (:tenant, now())"
)


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0088_channel_promo_codes.py"
    spec = importlib.util.spec_from_file_location("migration_channel_promo_codes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_previous_releases_writes_still_work_on_the_new_schema(
    db_session: AsyncSession,
) -> None:
    """An old worker's server-wide redemption and refused-attempt rows insert unchanged."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    code = await promo_store.insert_promo_code(
        db_session,
        code_hash="server",
        terms=build_promo_code_terms(amount_usd=Decimal("3"), timed=False),
    )
    assert code is not None
    params = {"code": code.id, "tenant": tenant.id, "account": account.id}
    first = cast(CursorResult[Any], await db_session.execute(OLD_SERVER_WIDE_INSERT, params))
    again = cast(CursorResult[Any], await db_session.execute(OLD_SERVER_WIDE_INSERT, params))
    await db_session.execute(OLD_FAILURE_INSERT, {"tenant": tenant.id})
    assert (first.rowcount, again.rowcount) == (1, 0), (
        "the old ON CONFLICT target still matches, and still keeps one row per tenant"
    )


@pytest.mark.fresh_schema
async def test_downgrade_drops_channel_codes_and_upgrade_admits_them_again(
    db_session: AsyncSession,
) -> None:
    """Downgrade keeps server-wide codes and redemptions; upgrade restores channel codes."""
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
    assert await promo_store.insert_redemption(
        db_session,
        promo_code_id=server.id,
        tenant_id=tenant.id,
        account_id=account.id,
        now=server.created_at,
        granted=True,
    )
    assert await promo_store.insert_channel_redemption(
        db_session,
        promo_code_id=channel.id,
        tenant_id=tenant.id,
        channel_id="c1",
        account_id=account.id,
        now=server.created_at,
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
    kept = await db_session.execute(text("SELECT count(*) FROM promo_redemptions"))
    assert kept.scalar_one() == 1, "the server-wide redemption stays"

    await conn.run_sync(upgrade)
    again = await promo_store.insert_promo_code(
        db_session,
        code_hash="again",
        terms=build_promo_code_terms(
            amount_usd=Decimal("3"), timed=False, channel_cap_usd=Decimal("3")
        ),
    )
    assert again is not None and again.channel_cap_usd == Decimal("3"), "channel codes are back"
