"""Private native lead queue; human decisions use the same core intake grants."""

from typing import cast
from uuid import UUID

from daimon.adapters.discord.checks import require_registered_guild, resolve_tenant_for_interaction
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.lead_intake import IntakeRecord, authorize_intake_actor
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.lead_intake import IntakeStore

import discord
from discord import Interaction, app_commands
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]


@app_commands.guild_only()
class LeadsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot: commands.Bot = bot

    @app_commands.command(name="leads", description="Private lead queue and review status")
    @require_registered_guild
    async def leads(self, interaction: BotInteraction, intake_id: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
        tenant = await resolve_tenant_for_interaction(interaction.client, interaction)
        fernet = runtime.turn_deps.fernet
        resolver = runtime.turn_deps.membership_resolver
        if tenant is None or fernet is None or resolver is None or interaction.guild is None:
            await interaction.followup.send("Lead intake is unavailable.", ephemeral=True)
            return
        guild = interaction.guild
        try:
            store = IntakeStore(runtime.sessionmaker, fernet, tenant)
            config, _ = await store.configuration()
            if str(guild.id) != config.guild_id:
                raise PermissionError("tenant_mismatch")
            async with runtime.sessionmaker() as session:
                policy = await load_access_policy(session, tenant_id=tenant)
            facts = await resolver.resolve(
                tenant, config.guild_id, str(interaction.user.id), require_fresh=True
            )
            authorize_intake_actor(
                config,
                policy,
                user_id=str(interaction.user.id),
                facts=facts,
                agent_id=config.agent_id,
                routine_id=None,
                execution_active=False,
            )
            target = await guild.fetch_channel(int(config.channel_id))
            member = await guild.fetch_member(interaction.user.id)
            if (
                not isinstance(target, discord.TextChannel)
                or target.permissions_for(target.guild.default_role).view_channel
                or not target.permissions_for(member).view_channel
            ):
                raise PermissionError("sales_channel_access_required")
            origin_ids = {str(interaction.channel_id)}
            if isinstance(interaction.channel, discord.Thread):
                origin_ids.add(str(interaction.channel.parent_id))
            if not authorize(
                policy,
                subject=Subject(),
                action=Action.READ_CHANNEL,
                place=Place(channel_id=config.channel_id),
                origin_channel_ids=frozenset(origin_ids),
            ):
                raise PermissionError("sales_queue_read_denied")
            guild._add_channel(target)  # pyright: ignore[reportPrivateUsage]

            async def visible(record: IntakeRecord) -> bool:
                thread_id = record.progress.discord_thread_id or next(
                    (e.remote_id for e in record.effects if e.step == "thread" and e.remote_id),
                    None,
                )
                if thread_id is None and any(e.step == "thread" for e in record.effects):
                    thread_id = record.progress.discord_message_id or next(
                        (
                            e.remote_id
                            for e in record.effects
                            if e.step == "starter" and e.remote_id
                        ),
                        None,
                    )
                    if thread_id is None:
                        return False
                if thread_id is None:
                    return True
                try:
                    thread = await guild.fetch_channel(int(thread_id))
                    if (
                        not isinstance(thread, discord.Thread)
                        or str(thread.parent_id) != config.channel_id
                    ):
                        return False
                    permissions = thread.permissions_for(member)
                    if not permissions.view_channel or not permissions.read_message_history:
                        return False
                    if (
                        thread.type is discord.ChannelType.private_thread
                        and not permissions.manage_threads
                    ):
                        await thread.fetch_member(member.id)
                    return bool(
                        authorize(
                            policy,
                            subject=Subject(),
                            action=Action.READ_CHANNEL,
                            place=Place(
                                channel_id=thread_id,
                                parent_channel_id=config.channel_id,
                                category_id=str(target.category_id) if target.category_id else None,
                            ),
                            origin_channel_ids=frozenset(origin_ids),
                        )
                    )
                except (discord.Forbidden, discord.NotFound):
                    return False

            # Private data never falls back to the invocation channel.
            if intake_id:
                record = await store.get(UUID(intake_id))
                if not await visible(record):
                    raise PermissionError("lead_thread_read_denied")
                title = record.submission.thread_title if record.submission else "Expired lead"
                content = f"{record.id} · {record.status}\n{title}\n{record.error_code or ''}"
                if record.progress.discord_thread_id:
                    content += f"\nhttps://discord.com/channels/{config.guild_id}/{record.progress.discord_thread_id}"
                content += (
                    "\nAsk Daimon to review this intake; approve exact CRM selections "
                    "in the conversation."
                )
            else:
                records = await store.list(limit=20, pending_only=True)
                records = [record for record in records if await visible(record)]
                content = "\n".join(f"{r.id} · {r.status} · {r.error_code or ''}" for r in records)
                content = content or "No pending leads."
            await interaction.followup.send(
                content[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            await interaction.followup.send(
                "Lead intake is unavailable or access was refused.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
