"""Human-only controls never accept stale or unauthorized decisions."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.recruitment import RecruitmentCog
from daimon.adapters.discord.recruitment_cards import review_card
from daimon.core.recruitment import Candidate, ThreadState


def test_cards_disable_incomplete_history_and_bind_revision() -> None:
    state = ThreadState(
        thread_id="20",
        parent_id="2",
        phase="review",
        revision="revision",
        candidate=Candidate(name="Synthetic"),
        card_intent="intent",
    )
    embed, view = review_card(state, None)
    assert embed.footer.text == "recruitment:intent"
    create = next(c for c in view.children if c.label == "Create")
    assert create.disabled and create.custom_id == "recruit:revision:create"
    state.scan_complete = True
    _, view = review_card(state, None)
    assert not next(c for c in view.children if c.label == "Create").disabled


async def test_nonrecruiter_click_is_ephemeral_and_never_confirms() -> None:
    service = SimpleNamespace(authorize=AsyncMock(side_effect=PermissionError), confirm=AsyncMock())

    @asynccontextmanager
    async def context(guild):
        yield service, None

    cog = RecruitmentCog(MagicMock(), MagicMock())
    cog.service = context
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 101
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 20
    interaction.user.id = 8
    interaction.data = {"custom_id": "recruit:revision:create"}
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    await cog.on_interaction(interaction)
    service.confirm.assert_not_awaited()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    assert "Recruiter access" in interaction.followup.send.await_args.args[0]


async def test_stale_click_refreshes_card_without_confirming() -> None:
    state = ThreadState(thread_id="20", parent_id="2", phase="review", revision="new", card_id="30")
    service = SimpleNamespace(
        authorize=AsyncMock(),
        store=SimpleNamespace(get=AsyncMock(return_value=state)),
        confirm=AsyncMock(),
    )

    @asynccontextmanager
    async def context(guild):
        yield service, None

    cog = RecruitmentCog(MagicMock(), MagicMock())
    cog.service = context
    cog.publish = AsyncMock()
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 101
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 20
    interaction.user.id = 7
    interaction.data = {"custom_id": "recruit:old:create"}
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    await cog.on_interaction(interaction)
    service.confirm.assert_not_awaited()
    cog.publish.assert_awaited_once()


async def test_restart_recovers_accepted_card_without_reposting() -> None:
    from daimon.core.recruitment import RecruitmentConfig

    state = ThreadState(
        thread_id="20", parent_id="2", phase="review", revision="r", card_intent="durable-intent"
    )

    @asynccontextmanager
    async def claim(thread):
        yield

    store = SimpleNamespace(get=AsyncMock(return_value=state), claim=claim, save=AsyncMock())
    service = SimpleNamespace(
        store=store, config=AsyncMock(return_value=RecruitmentConfig(guild_id="101", enabled=True))
    )
    bot = MagicMock()
    card = MagicMock(spec=discord.Message)
    card.id = 30
    card.author = bot.user
    card.embeds = [discord.Embed().set_footer(text="recruitment:durable-intent")]
    card.edit = AsyncMock()

    class History:
        def __aiter__(self):
            async def values():
                yield card

            return values()

    thread = MagicMock(spec=discord.Thread)
    thread.id = 20
    thread.history.return_value = History()
    thread.send = AsyncMock()
    cog = RecruitmentCog(bot, MagicMock())
    await cog.publish(thread, service)
    card.edit.assert_awaited_once()
    thread.send.assert_not_awaited()
    assert state.card_id == "30"


async def test_ambiguous_card_recovery_never_posts_elsewhere() -> None:
    from daimon.core.recruitment import RecruitmentConfig

    state = ThreadState(
        thread_id="20", parent_id="2", phase="review", revision="r", card_intent="durable-intent"
    )

    @asynccontextmanager
    async def claim(thread):
        yield

    store = SimpleNamespace(get=AsyncMock(return_value=state), claim=claim, save=AsyncMock())
    service = SimpleNamespace(
        store=store, config=AsyncMock(return_value=RecruitmentConfig(guild_id="101", enabled=True))
    )
    bot = MagicMock()
    card = MagicMock(spec=discord.Message)
    card.author = bot.user
    card.embeds = [discord.Embed().set_footer(text="recruitment:durable-intent")]

    class History:
        def __aiter__(self):
            async def values():
                yield card
                yield card

            return values()

    thread = MagicMock(spec=discord.Thread)
    thread.id = 20
    thread.history.return_value = History()
    thread.send = AsyncMock()
    cog = RecruitmentCog(bot, MagicMock())
    import pytest

    with pytest.raises(ValueError, match="card_recovery_incomplete"):
        await cog.publish(thread, service)
    thread.send.assert_not_awaited()
    bot.fetch_user.assert_not_called()


async def test_expired_card_redaction_runs_when_feature_is_disabled(monkeypatch) -> None:
    from uuid import uuid4

    from daimon.core.recruitment import RecruitmentConfig
    from pydantic import SecretStr

    state = ThreadState(thread_id="20", parent_id="2", phase="expired", card_id="30")
    config = RecruitmentConfig(guild_id="101", enabled=False)
    store = SimpleNamespace(
        configuration=AsyncMock(return_value=config),
        list_threads=AsyncMock(return_value=["20"]),
        get=AsyncMock(return_value=state),
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.recruitment.configured_tenants", AsyncMock(return_value=[uuid4()])
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.recruitment.RecruitmentStore", MagicMock(return_value=store)
    )
    port = SimpleNamespace(check_access=AsyncMock(), bot_id=AsyncMock(return_value="bot"))
    monkeypatch.setattr(
        "daimon.adapters.discord.recruitment.RecruitmentDiscord", MagicMock(return_value=port)
    )
    runtime = SimpleNamespace(
        turn_deps=SimpleNamespace(fernet=object()),
        sessionmaker=object(),
        settings=SimpleNamespace(
            recruitment=SimpleNamespace(enabled=False),
            discord=SimpleNamespace(bot_token=SecretStr("fake")),
        ),
    )
    bot = MagicMock()
    thread = MagicMock(spec=discord.Thread)
    bot.fetch_channel = AsyncMock(return_value=thread)
    cog = RecruitmentCog(bot, runtime)
    cog.redact_expired = AsyncMock()
    await cog.recover()
    cog.redact_expired.assert_awaited_once_with(thread, card_id="30")


async def test_expired_card_redaction_uses_retained_id_outside_recent_history() -> None:
    bot = MagicMock()
    card = MagicMock(spec=discord.Message)
    card.author = bot.user
    card.edit = AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.fetch_message = AsyncMock(return_value=card)
    cog = RecruitmentCog(bot, MagicMock())
    await cog.redact_expired(thread, card_id="30")
    card.edit.assert_awaited_once_with(content="Recruitment data expired.", embeds=[], view=None)
    thread.history.assert_not_called()


async def test_unconfigured_ingress_short_circuits_without_source_or_discord_io(
    monkeypatch,
) -> None:
    from daimon.adapters.discord.recruitment_guard import recruitment_message_blocked

    inactive = AsyncMock(return_value=False)
    source_filter = AsyncMock(side_effect=AssertionError("should not build source filter"))
    monkeypatch.setattr("daimon.adapters.discord.recruitment_guard.privacy_active", inactive)
    monkeypatch.setattr(
        "daimon.adapters.discord.recruitment_guard.recruitment_message_filter", source_filter
    )
    runtime = SimpleNamespace(sessionmaker=object())
    message = MagicMock(spec=discord.Message)
    message.guild.id = 101
    message.flags.has_thread = True
    message.guild.fetch_channel = AsyncMock()
    for _ in range(20):
        assert not await recruitment_message_blocked(runtime, message)
    source_filter.assert_not_awaited()
    message.guild.fetch_channel.assert_not_awaited()
