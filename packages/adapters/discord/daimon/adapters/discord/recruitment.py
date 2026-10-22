"""Staffing inputs and review controls in the existing Discord gateway."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Literal
from uuid import uuid4

import httpx
from daimon.adapters.discord.recruitment_cards import review_card
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.recruitment import may_decide
from daimon.core.recruitment_discord import RecruitmentDiscord
from daimon.core.recruitment_runtime import backend_client, build_service
from daimon.core.recruitment_service import RecruitmentService
from daimon.core.stores.recruitment import (
    RecruitmentBusy,
    RecruitmentStore,
    configured_tenants,
    invalidate_source,
)

import discord
from discord import app_commands
from discord.ext import commands

LiteralAction = Literal["status", "refresh", "retry", "discard"]


@app_commands.guild_only()
class RecruitmentCog(commands.Cog):
    def __init__(self, bot: commands.Bot, runtime: DiscordRuntime) -> None:
        self.bot, self.runtime = bot, runtime

    @asynccontextmanager
    async def service(
        self, guild_id: int
    ) -> AsyncIterator[tuple[RecruitmentService, RecruitmentDiscord]]:
        settings = self.runtime.settings
        fernet = self.runtime.turn_deps.fernet
        if not settings.recruitment.enabled or fernet is None or settings.discord is None:
            raise PermissionError("recruitment_unavailable")
        tenant = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
        store = RecruitmentStore(self.runtime.sessionmaker, fernet, tenant)
        async with httpx.AsyncClient() as http, backend_client(settings) as api_http:
            port = RecruitmentDiscord(http, settings.discord.bot_token.get_secret_value())
            yield build_service(settings, store, port, api_http, self.runtime.anthropic), port

    async def discover(self, thread: discord.Thread) -> None:
        try:
            async with self.service(thread.guild.id) as (service, port):
                config = await service.config()
                await port.check_access(
                    config,
                    str(thread.id),
                    str(self.bot.user.id) if self.bot.user else "",
                    recruiter=False,
                )
                await service.enroll(str(thread.id), str(thread.parent_id), thread.name)
        except (PermissionError, ValueError, LookupError, httpx.HTTPError, RecruitmentBusy):
            return

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self.discover(thread)

    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent) -> None:
        if not self.runtime.settings.recruitment.enabled:
            return
        try:
            channel = await self.bot.fetch_channel(payload.thread_id)
            if isinstance(channel, discord.Thread):
                await self.discover(channel)
        except (discord.NotFound, discord.Forbidden):
            return

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        await self.ingest_message(message.channel, message.id)

    async def raw_revision(
        self,
        guild_id: int | None,
        channel_id: int,
        message_id: int,
        *,
        edited_at: datetime | None = None,
        deleted: bool = False,
    ) -> None:
        if guild_id is None:
            return
        tenant = derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
        thread_id = await invalidate_source(
            self.runtime.sessionmaker,
            tenant,
            str(channel_id),
            str(message_id),
            observed_at=edited_at,
            deleted=deleted,
        )
        if thread_id is None or not self.runtime.settings.recruitment.enabled:
            return
        # The event is durable before the first Discord read. Failed enrichment
        # leaves confirmation/delivery fenced for reconciliation after restart.
        try:
            thread = await self.bot.fetch_channel(int(thread_id))
            if isinstance(thread, discord.Thread):
                if deleted:
                    async with self.service(guild_id) as (service, _):
                        await service.ingest(thread_id, [])
                else:
                    await self.ingest_message(thread, message_id, source_channel_id=str(channel_id))
        except (
            PermissionError,
            ValueError,
            LookupError,
            RecruitmentBusy,
            httpx.HTTPError,
            discord.HTTPException,
        ):
            return

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        raw_time = payload.data.get("edited_timestamp")
        edited = datetime.fromisoformat(raw_time) if isinstance(raw_time, str) else None
        await self.raw_revision(
            payload.guild_id, payload.channel_id, payload.message_id, edited_at=edited
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.raw_revision(
            payload.guild_id, payload.channel_id, payload.message_id, deleted=True
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        # Invalidate the entire batch before enriching or posting anything.
        for message_id in payload.message_ids:
            if payload.guild_id is not None:
                await invalidate_source(
                    self.runtime.sessionmaker,
                    derive_tenant_uuid(platform="discord", workspace_id=str(payload.guild_id)),
                    str(payload.channel_id),
                    str(message_id),
                    deleted=True,
                )

    async def ingest_message(
        self, thread: discord.Thread, message_id: int, *, source_channel_id: str | None = None
    ) -> None:
        try:
            async with self.service(thread.guild.id) as (service, port):
                if not await service.store.enrolled(str(thread.id)):
                    await service.enroll(str(thread.id), str(thread.parent_id), thread.name)
                if not await service.store.enrolled(str(thread.id)):
                    return
                config = await service.config()
                await port.check_access(
                    config, str(thread.id), await port.bot_id(), recruiter=False
                )
                source = await port.message(
                    config, source_channel_id or str(thread.id), str(message_id)
                )
                if source:
                    # Accept a live revision even while reconciliation holds a
                    # worker claim. Processing can safely resume next tick.
                    await service.store.ingest(str(thread.id), source)
                    await service.ingest(str(thread.id), [])
                await self.publish(thread, service)
        except (PermissionError, ValueError, LookupError, httpx.HTTPError, RecruitmentBusy):
            return

    async def publish(self, thread: discord.Thread, service: RecruitmentService) -> None:
        config = await service.config()
        async with service.store.claim(str(thread.id)):
            state = await service.store.get(str(thread.id))
            if state is None:
                return
            if state.phase == "expired":
                await self.redact_expired(thread, card_id=state.card_id)
                return
            item = await service.store.outbox(state.outbox_id) if state.outbox_id else None
            signature = hashlib.sha256(
                (
                    state.model_dump_json(exclude={"card_id", "card_revision"})
                    + (item.model_dump_json() if item else "")
                ).encode()
            ).hexdigest()
            if state.card_revision == signature:
                return
            if not state.card_intent:
                state.card_intent = uuid4().hex
                await service.store.save(state, config)
            marker = f"recruitment:{state.card_intent}"
            card = None
            if state.card_id:
                with suppress(discord.NotFound):
                    card = await thread.fetch_message(int(state.card_id))
            if card is None:
                history = [m async for m in thread.history(limit=200)]
                found = [
                    m
                    for m in history
                    if m.author == self.bot.user and any(e.footer.text == marker for e in m.embeds)
                ]
                if len(found) > 1 or not found and len(history) == 200:
                    raise ValueError("card_recovery_incomplete")
                card = found[0] if found else None
            embed, view = review_card(state, item)
            if card:
                await card.edit(
                    embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                card = await thread.send(
                    embed=embed,
                    view=view,
                    silent=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            state.card_id, state.card_revision = str(card.id), signature
            await service.store.save(state, config)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        data = interaction.data
        custom = str(data.get("custom_id", "")) if data else ""
        if not custom.startswith("recruit:") or interaction.guild_id is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            thread = interaction.channel
            if not isinstance(thread, discord.Thread):
                raise PermissionError("original_thread_required")
            async with self.service(interaction.guild_id) as (service, _port):
                actor, thread_id = str(interaction.user.id), str(thread.id)
                await service.authorize(actor, thread_id)
                state = await service.store.get(thread_id)
                parts = custom.split(":", 2)
                if (
                    not state
                    or len(parts) != 3
                    or parts[1] != state.revision
                    or not interaction.message
                    or str(interaction.message.id) != state.card_id
                ):
                    await self.publish(thread, service)
                    raise ValueError("Summary changed; use the refreshed review.")
                action = parts[2]
                if action == "correct":
                    await interaction.followup.send(
                        "Post explicit corrected details here, then use /recruitment refresh. "
                        "Review the new card before confirming.",
                        ephemeral=True,
                    )
                    return
                if action == "retry":
                    await service.deliver(thread_id, force=True)
                elif action == "discard":
                    await service.discard(thread_id, actor)
                else:
                    await service.confirm(thread_id, parts[1], action, actor, str(interaction.id))
                    await service.deliver(thread_id)
                await self.publish(thread, service)
                await interaction.followup.send(
                    "Decision recorded; check the thread status.", ephemeral=True
                )
        except PermissionError:
            await interaction.followup.send(
                "Recruiter access to the original thread is required.", ephemeral=True
            )
        except (ValueError, RecruitmentBusy):
            await interaction.followup.send(
                "The review changed or needs attention. Use the latest card or refresh the review.",
                ephemeral=True,
            )
        except (httpx.HTTPError, discord.HTTPException):
            await interaction.followup.send(
                "Recruitment is unavailable. "
                "Retry in the original thread after access is restored.",
                ephemeral=True,
            )

    @app_commands.command(
        name="recruitment", description="Private staffing status, refresh or recovery"
    )
    @app_commands.describe(
        action="status, refresh, retry or discard", thread_id="Original thread ID for a paused item"
    )
    async def recruitment(
        self,
        interaction: discord.Interaction,
        action: LiteralAction = "status",
        thread_id: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("A guild is required.", ephemeral=True)
            return
        try:
            async with self.service(interaction.guild.id) as (service, _port):
                config = await service.config()
                member = await interaction.guild.fetch_member(interaction.user.id)
                if not may_decide(
                    config, str(member.id), tuple(str(r.id) for r in member.roles), can_access=True
                ):
                    raise PermissionError
                if action == "status":
                    states = [
                        await service.store.get(t) for t in await service.store.list_threads()
                    ]
                    text = "\n".join(
                        f"{s.thread_id}: {s.phase}; {s.error or 'ready'}" for s in states if s
                    )
                    await interaction.followup.send(
                        text[:1900] or "No staffing threads.", ephemeral=True
                    )
                    return
                target = thread_id or str(interaction.channel_id)
                await service.authorize(str(member.id), target)
                channel = await self.bot.fetch_channel(int(target))
                if not isinstance(channel, discord.Thread):
                    raise PermissionError
                if action == "retry":
                    await service.deliver(target, force=True)
                elif action == "discard":
                    await service.discard(target, str(member.id))
                else:
                    await service.refresh(target, str(member.id))
                await self.publish(channel, service)
                await interaction.followup.send(
                    "Recruitment status refreshed in the original thread.", ephemeral=True
                )
        except (
            PermissionError,
            ValueError,
            RecruitmentBusy,
            httpx.HTTPError,
            discord.HTTPException,
        ):
            await interaction.followup.send(
                "Recruitment unavailable or recruiter/thread access refused.", ephemeral=True
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.recover()

    async def redact_expired(self, thread: discord.Thread, *, card_id: str | None = None) -> None:
        # The non-content card ID survives erasure, so cleanup does not depend
        # on the candidate card remaining in the recent history window.
        if card_id:
            with suppress(discord.NotFound):
                message = await thread.fetch_message(int(card_id))
                if message.author == self.bot.user:
                    await message.edit(content="Recruitment data expired.", embeds=[], view=None)
            return
        async for message in thread.history(limit=200):
            if message.author == self.bot.user and any(
                (e.footer.text or "").startswith("recruitment:") for e in message.embeds
            ):
                await message.edit(content="Recruitment data expired.", embeds=[], view=None)

    async def recover(self) -> None:
        fernet = self.runtime.turn_deps.fernet
        settings = self.runtime.settings
        if fernet is None or settings.discord is None:
            return
        for tenant in await configured_tenants(self.runtime.sessionmaker):
            store = RecruitmentStore(self.runtime.sessionmaker, fernet, tenant)
            config = await store.configuration()
            if not config:
                continue
            for id in await store.list_threads():
                try:
                    state = await store.get(id)
                    if state and state.phase == "expired":
                        async with httpx.AsyncClient() as http:
                            port = RecruitmentDiscord(
                                http, settings.discord.bot_token.get_secret_value()
                            )
                            await port.check_access(
                                config, id, await port.bot_id(), recruiter=False
                            )
                            channel = await self.bot.fetch_channel(int(id))
                            if isinstance(channel, discord.Thread):
                                await self.redact_expired(channel, card_id=state.card_id)
                        continue
                    if not settings.recruitment.enabled or not config.enabled:
                        continue
                    async with self.service(int(config.guild_id)) as (service, port):
                        await port.check_access(config, id, await port.bot_id(), recruiter=False)
                        channel = await self.bot.fetch_channel(int(id))
                        if isinstance(channel, discord.Thread):
                            await self.publish(channel, service)
                except (
                    PermissionError,
                    ValueError,
                    RecruitmentBusy,
                    httpx.HTTPError,
                    discord.HTTPException,
                ):
                    continue
