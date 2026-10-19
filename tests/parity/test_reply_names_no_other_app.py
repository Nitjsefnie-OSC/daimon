"""A model reply that names another chat app is posted without the name, on every platform.

The model is told never to name another chat app, but it still does ("Never upload
customer data to Discord or Slack." repeated back on Teams), so what Daimon posts is
scrubbed too. This drives the real entry point of each platform with a scripted reply.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import build_turn_router
from .drivers.protocol import PlatformDriver, platform_ids

#: One reply that names every app; each platform may keep only its own name.
_REPLY = "Never upload customer data to Discord or Slack. Microsoft Teams is no better."
_POSTED = {
    "discord": "Never upload customer data to Discord or another app. Another app is no better.",
    "slack": "Never upload customer data to another app or Slack. Another app is no better.",
    "teams": "Never upload customer data to another app. Microsoft Teams is no better.",
}


async def test_the_posted_reply_never_names_another_chat_app(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900001001, user=555000111, channel=100000
    )
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()

    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=build_turn_router(str(tenant.id), agent_text=_REPLY),
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="what did I just say?",
    )

    expected = _POSTED[driver.param_id]
    assert any(expected in text for text in posted), posted
    own = {"discord": "Discord", "slack": "Slack", "teams": "Teams"}
    foreign = [name for platform, name in own.items() if platform != driver.param_id]
    for text in posted:
        for name in foreign:
            assert name not in text, f"{driver.param_id} posted {name}: {text!r}"
