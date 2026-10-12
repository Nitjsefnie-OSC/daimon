"""Discord's mirror poster, driven through the core outbox poll."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.mirror_delivery import make_discord_mirror_poster
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import MirrorsSettings
from daimon.core.mirror_delivery import poll_mirror_deliveries_once, record_conversation_event
from daimon.core.mirror_events import ActorRef, ConversationEventV1, SourceRef
from daimon.core.stores import mirrors as store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import MirrorRow
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = 424242
_CHANNEL = 555
_AGENT = uuid.uuid4()
_NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _channel(*, guild_id: int = _GUILD) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = _CHANNEL
    channel.guild = SimpleNamespace(id=guild_id)
    channel.category_id = None
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id, thread.send = 777, _CHANNEL, AsyncMock()
    channel.thread = thread
    header = MagicMock()
    header.create_thread = AsyncMock(return_value=thread)
    channel.send = AsyncMock(return_value=header)
    return channel


async def _setup(db_session: AsyncSession, policy: TenantAccessPolicy | None = None) -> MirrorRow:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    mirror = await store.upsert_mirror(
        db_session,
        tenant_id=tenant.id,
        name="qa",
        source_agent_id=_AGENT,
        source_agent_name="helper",
        source_agent_names=("helper",),
        dest_platform="discord",
        dest_kind="channel",
        dest_id=str(_CHANNEL),
        consent_note=None,
        max_answer_chars=None,
        account_id=None,
        now=_NOW,
    )
    await db_session.commit()
    return mirror


async def _ask_and_poll(
    sm: async_sessionmaker[AsyncSession], mirror: MirrorRow, channel: MagicMock
) -> None:
    event = ConversationEventV1(
        id=uuid.uuid4(),
        surface="agent_chat",
        occurred_at=_NOW,
        tenant_id=mirror.tenant_id,
        agent_id=_AGENT,
        source=SourceRef(channel_id="300", session_key="sesn_abc123"),
        actor=ActorRef(display_name="Ada"),
        question="Ping @everyone?",
        answer="Pong.",
    )
    await record_conversation_event(sm, event, now=_NOW)

    async def fetch(channel_id: int) -> object:
        return channel if channel_id == _CHANNEL else channel.thread

    poster = make_discord_mirror_poster(sm, fetch_channel=fetch, max_answer_chars=3000)
    await poll_mirror_deliveries_once(
        sm, platform="discord", post=poster, settings=MirrorsSettings(), now=_NOW
    )


async def test_a_session_opens_one_thread_and_posts_without_mentions(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mirror = await _setup(db_session)
    channel = _channel()

    await _ask_and_poll(db_session_factory, mirror, channel)
    await _ask_and_poll(db_session_factory, mirror, channel)

    assert channel.send.await_count == 1, "the session's thread is opened once, then reused"
    channel.send.return_value.create_thread.assert_awaited_once_with(name="helper · abc123")
    sent = channel.thread.send.await_args_list
    assert [call.kwargs["content"].split("\n")[1] for call in sent] == [
        "Ping @everyone?",
        "Pong.",
    ] * 2, sent
    every_call = [*sent, *channel.send.await_args_list]
    assert all(call.kwargs["allowed_mentions"].to_dict() == {"parse": []} for call in every_call), (
        "a mirror never pings anyone"
    )


async def test_a_channel_of_another_server_pauses_the_mirror(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mirror = await _setup(db_session)
    channel = _channel(guild_id=1)

    await _ask_and_poll(db_session_factory, mirror, channel)

    channel.send.assert_not_awaited()
    paused = await store.get_mirror(db_session, mirror.id)
    assert paused is not None and paused.paused_reason == "destination_unavailable", paused


async def test_a_rule_letting_nobody_post_pauses_the_mirror(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    policy = TenantAccessPolicy(channel_rules={str(_CHANNEL): ChannelRule(writers="none")})
    mirror = await _setup(db_session, policy)
    channel = _channel()

    await _ask_and_poll(db_session_factory, mirror, channel)

    channel.send.assert_not_awaited()
    paused = await store.get_mirror(db_session, mirror.id)
    assert paused is not None and paused.paused_reason == "writers_none", paused
