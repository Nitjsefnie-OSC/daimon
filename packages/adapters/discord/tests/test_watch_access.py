"""Watch ACLs use REST roles and thread state, even when gateway caches disagree."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.watch_access import require_watch_thread_access
from daimon.core.stores import watches
from daimon.core.turn.errors import AdmissionDenied

from .watch_fixtures import enrolled_page_watch


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    "mode",
    [
        "stale_view",
        "stale_history",
        "stale_manager",
        "stale_admin",
        "roles_failed",
        "locked",
        "locked_manager",
        "fresh_manager",
    ],
)
async def test_watch_uses_rest_roles_and_locked_state(db_session_factory, monkeypatch, mode):
    factory, _, _, _, _, watch, key = await enrolled_page_watch(db_session_factory, monkeypatch)
    from daimon.core.stores.task_continuations import get_continuation

    async with factory() as db:
        row = await get_continuation(db, idempotency_key=key)
    state = MagicMock()
    allowed = discord.Permissions(
        view_channel=True, read_message_history=True, send_messages_in_threads=True
    )
    cached = discord.Permissions(allowed.value)
    cached.manage_threads = True
    cached.administrator = mode == "stale_admin"
    role_data = {"id": "77", "name": "reader", "permissions": str(cached.value)}
    guild = discord.Guild(
        data={
            "id": "710000099",
            "name": "test",
            "owner_id": "1",
            "roles": [
                {"id": "710000099", "name": "@everyone", "permissions": "0"},
                role_data,
            ],
        },
        state=state,
    )
    user_data = {"id": "555", "username": "owner", "discriminator": "0", "avatar": None}
    state.store_user.return_value = discord.User(state=state, data=user_data)
    member = discord.Member(
        data={"user": user_data, "roles": ["77"], "flags": 0}, guild=guild, state=state
    )
    parent = discord.TextChannel(
        state=state,
        guild=guild,
        data={
            "id": "4241",
            "type": 0,
            "name": "parent",
            "position": 0,
            "permission_overwrites": [],
        },
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id = 4242
    thread.parent_id = 4241
    thread.guild = guild
    thread.type = discord.ChannelType.private_thread
    thread.locked = mode in {"locked", "locked_manager"}
    thread.fetch_member = AsyncMock(return_value=MagicMock())
    if mode == "stale_view":
        allowed.view_channel = False
    if mode == "stale_history":
        allowed.read_message_history = False
    if mode in {"locked_manager", "fresh_manager"}:
        allowed.manage_threads = True
    if mode in {"stale_manager", "stale_admin", "fresh_manager"}:
        thread.fetch_member.side_effect = discord.NotFound(
            MagicMock(status=404, reason="gone"), "Unknown Member"
        )
    fresh_role = discord.Role(
        state=state, guild=guild, data=role_data | {"permissions": str(allowed.value)}
    )
    monkeypatch.setattr(discord.Guild, "fetch_member", AsyncMock(return_value=member))
    monkeypatch.setattr(
        discord.Guild, "fetch_roles", AsyncMock(return_value=[guild.default_role, fresh_role])
    )
    monkeypatch.setattr(
        discord.Guild,
        "fetch_channel",
        AsyncMock(side_effect=lambda channel_id: thread if channel_id == thread.id else parent),
    )
    if mode == "roles_failed":
        guild.fetch_roles.side_effect = discord.Forbidden(
            MagicMock(status=403, reason="denied"), "denied"
        )
    # Cache grants Manage Threads (and possibly Administrator) throughout.
    assert member.guild_permissions.manage_threads
    if mode in {"locked_manager", "fresh_manager"}:
        verified = await require_watch_thread_access(factory, row, thread)
        assert verified.guild_permissions.manage_threads
        thread.fetch_member.assert_not_awaited()
    else:
        with pytest.raises(AdmissionDenied):
            await require_watch_thread_access(factory, row, thread)
        async with factory() as db:
            paused = await watches.get_watch(db, watch.id)
        assert paused.status == "paused"
        assert paused.source_state["reason"].startswith("Watch paused:")
    assert member.guild_permissions.manage_threads  # Original guild cache was never mutated.
