"""First-party lead intake tools; authority derives only from live server context."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import discord
import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.services.lead_intake import HttpIntakePorts
from daimon.adapters.mcp.tools._channel_policy import (
    list_running_origins,
    load_channel_policy,
    require_channel_writable,
)
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.adapters.mcp.tools.discord._client import (
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _check_view_permission,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.lead_intake import (
    IntakeRecord,
    IntakeResolution,
    LeadDraft,
    LeadSource,
    authorize_intake_actor,
)
from daimon.core.lead_intake_service import IntakeService, VerifiedSource
from daimon.core.permissions import home_of
from daimon.core.stores.accounts import account_exists
from daimon.core.stores.lead_intake import IntakeStore, execution_is_active
from daimon.core.stores.routines import get_routine
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@asynccontextmanager
async def service(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    resolve: bool = False,
    draft: LeadDraft | None = None,
) -> AsyncIterator[IntakeService]:
    if (
        runtime.fernet is None
        or runtime.settings.discord is None
        or auth.platform != "discord"
        or not auth.platform_user_id
        or not auth.external_id
        or auth.is_operator
        or auth.agent_id is not None
    ):
        raise ToolError("lead_intake_requires_authenticated_discord_chat")
    store = IntakeStore(runtime.session_factory, runtime.fernet, auth.tenant_id)
    try:
        config, credentials = await store.configuration()
    except ValueError as exc:
        raise ToolError("lead_intake_not_configured") from exc
    if auth.external_id != config.guild_id or auth.chat_agent_id != config.agent_id:
        raise ToolError("lead_intake_agent_or_tenant_mismatch")
    token = runtime.settings.discord.bot_token.get_secret_value()

    async def reauthorize(source_ref: LeadSource | None = None) -> None:
        async with runtime.session_factory() as session:
            if not await account_exists(session, account_id=auth.account_id):
                raise ToolError("actor_account_revoked")
        policy = await load_channel_policy(runtime, auth)
        resolver = runtime.membership_resolver
        if resolver is None:
            raise ToolError("membership_unavailable")
        facts = await resolver.resolve(
            auth.tenant_id, config.guild_id, auth.platform_user_id or "", require_fresh=True
        )
        active = False
        row = None
        routine = auth.routine_id is not None or auth.execution_id is not None
        if routine:
            async with runtime.session_factory() as session:
                row = (
                    await get_routine(session, auth.routine_id, tenant_id=auth.tenant_id)
                    if auth.routine_id
                    else None
                )
                active = (
                    auth.routine_id is not None
                    and auth.execution_id is not None
                    and await execution_is_active(
                        session, auth.tenant_id, auth.routine_id, auth.execution_id
                    )
                )
            if (
                not active
                or row is None
                or not row.enabled
                or row.created_by_user_id != auth.platform_user_id
                or config.routine_id != auth.routine_id
                or resolve
                or (source_ref is not None and source_ref.kind != "gmail")
            ):
                raise ToolError("routine_not_authorized")
            # Sealed sources cannot be exported to a CRM by a scheduled agent.
            if home_of(
                policy, row.source_thread_id or row.source_channel_id, row.source_channel_id
            ):
                raise ToolError("isolated_source_export_denied")
        try:
            authorize_intake_actor(
                config,
                policy,
                user_id=auth.platform_user_id or "",
                facts=facts,
                agent_id=auth.chat_agent_id,
                routine_id=auth.routine_id,
                execution_active=active if routine else False,
                resolve=resolve,
            )
        except PermissionError as exc:
            raise ToolError(str(exc)) from None
        origins = await list_running_origins(runtime, auth)
        if not routine and not origins:
            raise ToolError("interactive_origin_required")
        if resolve and (
            len(origins) != 1
            or await session_card_gap(runtime, auth, origins[0], tool_name="lead_intake_resolve")
            is not None
        ):
            raise ToolError("human_resolution_approval_required")
        if any(home_of(policy, o.parent_channel_id, o.thread_id) for o in origins):
            raise ToolError("isolated_source_export_denied")
        origin_ids = {value for o in origins for value in (o.parent_channel_id, o.thread_id)}
        if routine and row is not None:
            origin_ids.update(
                value for value in (row.source_channel_id, row.source_thread_id) if value
            )
        if not authorize(
            policy,
            subject=Subject(),
            action=Action.READ_CHANNEL,
            place=Place(channel_id=config.channel_id),
            origin_channel_ids=frozenset(origin_ids),
        ):
            raise ToolError("sales_queue_read_denied")
        # Native permissions and category/agent policy are re-read before every effect.
        async with rest_client(token) as client:
            _, member = await _resolve_member(client, config.guild_id, auth.platform_user_id or "")
            target = await client.fetch_channel(int(config.channel_id))
            if (
                not isinstance(target, discord.TextChannel)
                or str(target.guild.id) != config.guild_id
            ):
                raise ToolError("sales_channel_required")
            if target.permissions_for(target.guild.default_role).view_channel:
                raise ToolError("restricted_sales_channel_required")
            _check_view_permission(target, member)
            _check_send_permission(target, member)
            await require_channel_writable(
                runtime,
                auth,
                channel_id=config.channel_id,
                category_id=str(target.category_id) if target.category_id else None,
                policy=policy,
            )
            if source_ref is not None and source_ref.kind == "manual":
                source = await client.fetch_channel(int(source_ref.channel_id or "0"))
                if (
                    not isinstance(source, discord.TextChannel | discord.Thread)
                    or str(source.guild.id) != config.guild_id
                ):
                    raise ToolError("manual_source_channel_required")
                parent_id = None
                if isinstance(source, discord.Thread):
                    await _ensure_thread_parent_cached(source)
                    await _check_thread_view(client, source, member, auth.platform_user_id or "")
                    parent_id = str(source.parent_id)
                _check_view_permission(source, member)
                decision = authorize(
                    policy,
                    subject=Subject(),
                    action=Action.READ_CHANNEL,
                    place=Place(channel_id=str(source.id), parent_channel_id=parent_id),
                    origin_channel_ids=frozenset(
                        value
                        for origin in origins
                        for value in (origin.parent_channel_id, origin.thread_id)
                    ),
                )
                if not decision or home_of(policy, str(source.id), parent_id):
                    raise ToolError("source_export_denied")

    await reauthorize(draft.source if draft else None)
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        refresh = None
        if credentials.get("gmail_refresh_token"):
            refresh = {
                "refresh_token": credentials["gmail_refresh_token"],
                "client_id": credentials["gmail_client_id"],
                "client_secret": credentials["gmail_client_secret"],
            }
        ports = HttpIntakePorts(
            client,
            gmail_token=credentials.get("gmail_access_token", ""),
            hubspot_token=credentials["hubspot_access_token"],
            discord_token=token,
            gmail_refresh_credentials=refresh,
        )
        provenance = (
            (auth.routine_id, auth.execution_id) if auth.routine_id and auth.execution_id else None
        )
        yield IntakeService(store, ports, reauthorize, provenance=provenance)


def register_lead_intake_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord"})
    async def lead_intake_submit(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, draft: LeadDraft
    ) -> IntakeRecord:
        """Verify and durably register one classified Gmail or own explicit Discord lead.

        Classify spam and out-of-scope messages as other. Capture has no external
        effects. The service posts a title and threaded dossier, writes only a
        draft reply, conservatively reuses/creates CRM records, then archives
        Gmail after dossier delivery. Replay the same immutable source on timeout.
        """
        auth = await _auth(ctx)
        async with service(runtime, auth, draft=draft) as intake:
            return await intake.submit(draft, auth.platform_user_id)

    @mcp.tool(tags={"discord"})
    async def lead_intake_resume(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, intake_id: UUID | None = None
    ) -> dict[str, object]:
        """Recover lead effects and mailbox coverage without blindly retrying uncertain creates.

        Without an ID, resumes up to 25 pending records and scans archived mail
        from the operator checkpoint. Inspect scan errors and remaining work.
        """
        async with service(runtime, await _auth(ctx)) as intake:
            if intake_id:
                return {"intake": (await intake.resume(intake_id)).model_dump(mode="json")}
            pending = await intake.store.list(limit=26, pending_only=True)
            results = [await intake.resume(r.id) for r in pending[:25]]
            return {
                "intakes": [r.model_dump(mode="json") for r in results],
                "has_more_pending": len(pending) > 25,
                "mailbox_scan": await intake.scan(),
            }

    @mcp.tool(tags={"discord"})
    async def lead_intake_read_source(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, message_id: str
    ) -> VerifiedSource:
        """Read attachment-free original Gmail text for an unexpired discovered message.

        Run resume to discover mail, read each source here, then classify and
        submit its immutable source ID. Uses the configured tenant mailbox;
        credentials never enter the result. Does not post, archive or change CRM.
        """
        async with service(runtime, await _auth(ctx)) as intake:
            return await intake.read_source(message_id)

    @mcp.tool(tags={"discord"})
    async def lead_intake_resolve(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, intake_id: UUID, resolution: IntakeResolution
    ) -> IntakeRecord:
        """Apply evidence-backed human CRM selections as a sales resolver or core-role member.

        Show exact records and proposed writes before approval. Routines never
        resolve. Completed identities are immutable; uncertain creates must be
        recovered before selecting replacements. Server admin alone is no grant.
        """
        auth = await _auth(ctx)
        async with service(runtime, auth, resolve=True) as intake:
            return await intake.resolve(intake_id, resolution, auth.platform_user_id or "")

    @mcp.tool(tags={"discord"})
    async def lead_intake_get(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, intake_id: UUID
    ) -> IntakeRecord:
        """Read one authorized tenant lead, its review state and durable effect progress."""
        async with service(runtime, await _auth(ctx)) as intake:
            return await intake.store.get(intake_id)

    @mcp.tool(tags={"discord"})
    async def lead_intake_list(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        after_id: UUID | None = None,
    ) -> list[IntakeRecord]:
        """Read up to 100 tenant leads in the restricted sales queue, oldest first."""
        async with service(runtime, await _auth(ctx)) as intake:
            return await intake.store.list(after_id=after_id)

    @mcp.tool(tags={"discord"})
    async def lead_intake_health(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> dict[str, object]:
        """Read tenant lead counts, oldest pending age and mailbox coverage."""
        async with service(runtime, await _auth(ctx)) as intake:
            config, _ = await intake.store.configuration()
            state = await intake.store.mailbox(config.source_connection_id)
            return {**await intake.store.health(), "mailbox": state.model_dump(mode="json")}
