"""Owner-bound watch management. Secrets travel only in a private owner DM."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_dm_recipient_allowed
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.adapters.mcp.watch_polling import authorize_poll
from daimon.core.config import DirectMessagePolicy
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.membership_subject import resolve_subject
from daimon.core.stores import watches
from daimon.core.stores.domain import ChatPlatform, WatchRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel


class WatchSummary(BaseModel):
    watch_id: uuid.UUID
    source: str
    status: str
    reason: str | None
    thread_id: str
    parent_channel_id: str
    agent_id: str
    expires_at: datetime
    key_id: str | None = None


def summary(row: WatchRow, *, key_id: str | None = None) -> WatchSummary:
    reason = row.source_state.get("reason")
    return WatchSummary(
        watch_id=row.id,
        source=row.source,
        status=row.status,
        reason=str(reason) if reason else None,
        thread_id=row.thread_id,
        parent_channel_id=row.parent_channel_id,
        agent_id=row.agent_id,
        expires_at=row.expires_at,
        key_id=key_id,
    )


async def private_key_delivery(
    runtime: McpRuntime,
    auth: AuthIdentity,
    watch: WatchRow,
    *,
    key_id: str,
    key: str,
) -> None:
    """Never return plaintext keys to an MCP result or post into a conversation."""
    policy = runtime.settings.direct_message_policies.get(watch.tenant_id, DirectMessagePolicy())
    if not policy.allows(watch.owner_external_id):
        raise ToolError("Private owner delivery is disabled")
    await require_dm_recipient_allowed(runtime, auth, recipient_id=watch.owner_external_id)
    async with rest_client(_require_bot_token(runtime)) as client:
        # Guild membership is fetched live, including for an admin rotating another owner's key.
        if auth.external_id is None:
            raise ToolError("Missing workspace")
        guild = await client.fetch_guild(int(auth.external_id))
        member = await guild.fetch_member(int(watch.owner_external_id))
        if member.bot:
            raise ToolError("Watch keys require a human owner")
        dm = await member.create_dm()
        await dm.send(
            f"Daimon OS page signing key for watch {watch.id}, page {watch.page_id}:\n"
            f"key_id={key_id}\nkey={key}\n"
            "Install privately on the OS page host and sidecar. This key is shown once.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def fresh_admin(runtime: McpRuntime, auth: AuthIdentity) -> bool:
    if not auth.is_admin:
        return False
    if auth.platform != "discord" or auth.platform_user_id is None:
        return False
    subject = await resolve_subject(
        runtime.session_factory,
        runtime.membership_resolver,
        tenant_id=auth.tenant_id,
        user_id=auth.platform_user_id,
        require_fresh=True,
    )
    return subject.verified_membership is True and subject.is_admin


async def create_watch_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    instructions: str,
    source: Literal["page_event", "poll"],
    page_id: str | None = None,
    tool: str = "read_thread",
    arguments: dict[str, object] | None = None,
    projection: str = ".",
    rule: str = "change",
    interval: int = 300,
    lifetime_days: int = 7,
) -> WatchSummary:
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if not origin.attended or auth.platform != "discord" or auth.platform_user_id is None:
        raise ToolError("Create a watch from an attended Discord conversation")
    if not 1 <= lifetime_days <= 30:
        raise ToolError("Watch lifetime must be 1 to 30 days")
    now = datetime.now(UTC)
    binding = ContinuationRequest(
        tenant_id=auth.tenant_id,
        platform=cast(ChatPlatform, auth.platform),
        parent_channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        requester_account_id=auth.account_id,
        requester_external_user_id=auth.platform_user_id,
        target_ma_agent_id=origin.responder_ma_agent_id,
        target_name=origin.responder_name,
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    try:
        async with runtime.session_factory.begin() as db:
            if source == "poll":
                watch = await watches.register_poll_watch(
                    db,
                    binding,
                    instructions=instructions,
                    tool=tool,
                    arguments=arguments or {"thread_id": origin.thread_id},
                    projection=projection,
                    rule=rule,
                    interval=interval,
                    expires_at=now + timedelta(days=lifetime_days),
                    now=now,
                )
            else:
                if page_id is None:
                    raise ValueError("An OS page UUID is required")
                watch = await watches.register_page_watch(
                    db,
                    binding,
                    instructions=instructions,
                    page_id=uuid.UUID(page_id),
                    projection=projection,
                    expires_at=now + timedelta(days=lifetime_days),
                    now=now,
                )
            # Current owner/agent/seal policy, independently of the origin grant.
            await authorize_poll(runtime, watch)
        if source == "page_event":
            return await rotate_watch_impl(runtime, auth, watch_id=str(watch.id))
        return summary(watch)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


async def rotate_watch_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, watch_id: str
) -> WatchSummary:
    try:
        key_id, key = secrets.token_hex(12), secrets.token_urlsafe(48)
        admin = await fresh_admin(runtime, auth)
        async with runtime.session_factory.begin() as db:
            watch = await watches.require_owned_watch(
                db,
                uuid.UUID(watch_id),
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                is_admin=admin,
            )
            if watch.source != "page_event":
                raise ValueError("Poll watches have no signing key")
            await watches.rotate_signing_key(
                db, watch.id, key_id=key_id, key=key, expires_at=watch.expires_at
            )
        try:
            await private_key_delivery(runtime, auth, watch, key_id=key_id, key=key)
        except Exception:
            async with runtime.session_factory.begin() as db:
                await watches.pause_watch(db, watch.id, reason="key_delivery_failed")
            raise ToolError(
                "Private key delivery failed; watch paused. Rotate again to retry."
            ) from None
        return summary(watch, key_id=key_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


async def manage_watch_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    watch_id: str,
    action: Literal["show", "pause", "resume", "delete"],
) -> WatchSummary:
    try:
        admin = await fresh_admin(runtime, auth)
        async with runtime.session_factory.begin() as db:
            row = await watches.require_owned_watch(
                db,
                uuid.UUID(watch_id),
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                is_admin=admin,
            )
            now = datetime.now(UTC)
            if action == "pause":
                await watches.pause_watch(db, row.id)
            elif action == "resume":
                await watches.resume_watch(db, row.id, now=now)
                await authorize_poll(runtime, row)
            elif action == "delete":
                await watches.delete_watch(db, row.id, now=now)
                return summary(row.model_copy(update={"status": "deleted"}))
            updated = await watches.get_watch(db, row.id)
            assert updated is not None
            return summary(updated)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def register_watch_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def create_watch(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        instructions: str,
        source: Literal["page_event", "poll"],
        page_id: str | None = None,
        tool: str = "read_thread",
        arguments: dict[str, object] | None = None,
        projection: str = ".",
        rule: Literal["change", "added", "updated"] = "change",
        interval: int = 300,
        lifetime_days: int = 7,
    ) -> WatchSummary:
        """Watch an OS page event or a reviewed OS read tool from this conversation.

        Binds the current human owner, tenant, agent and exact Discord thread.
        Requires an attended turn origin. Polls run without an AI turn; firings
        use unattended read-and-report turns and current access/spend checks.
        Only read_thread and read_channel are pollable; no URLs or outside tools.
        Projection supports `.`, `.field.nested` and `.rows[0].field`; other jq
        expressions are refused. Polls start with a saved baseline and compare
        change, added array elements, or updated object keys every 60–86400 seconds.
        Lifetime is 1–30 days. Producers remain disabled until both operator gates
        are enabled. Page signing keys are sent privately to the owner once and
        never returned here; install them on the OS host and sidecar privately.
        """
        return await create_watch_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            instructions=instructions,
            source=source,
            page_id=page_id,
            tool=tool,
            arguments=arguments,
            projection=projection,
            rule=rule,
            interval=interval,
            lifetime_days=lifetime_days,
        )

    @mcp.tool
    async def list_watches(ctx: Context) -> list[WatchSummary]:  # pyright: ignore[reportUnusedFunction]
        """List your watches in this workspace, including paused imports and expiry; no signing
        secrets.
        """
        auth = await _auth(ctx)
        async with runtime.session_factory() as db:
            return [
                summary(row)
                for row in await watches.list_owned_watches(
                    db, tenant_id=auth.tenant_id, account_id=auth.account_id
                )
            ]

    @mcp.tool
    async def show_watch(ctx: Context, watch_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Show your watch's immutable destination, source, status and expiry. Admins may inspect
        within their workspace.
        """
        return await manage_watch_impl(runtime, await _auth(ctx), watch_id=watch_id, action="show")

    @mcp.tool
    async def pause_watch(ctx: Context, watch_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Pause your watch and cancel its queued wakes. Workspace admins may pause another
        owner's watch.
        """
        return await manage_watch_impl(runtime, await _auth(ctx), watch_id=watch_id, action="pause")

    @mcp.tool
    async def resume_watch(ctx: Context, watch_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Resume your paused watch after fresh access checks. Imported watches require operator
        confirmation; source_unmapped never resumes.
        """
        return await manage_watch_impl(
            runtime, await _auth(ctx), watch_id=watch_id, action="resume"
        )

    @mcp.tool
    async def rotate_watch_key(ctx: Context, watch_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Rotate an OS page signing key, revoke the old key and cancel queued wakes. The fresh
        key goes to its owner by private DM once.
        """
        return await rotate_watch_impl(runtime, await _auth(ctx), watch_id=watch_id)

    @mcp.tool
    async def delete_watch(ctx: Context, watch_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Delete your watch and its retained evidence after cancelling queued wakes; admins may
        delete within their workspace.
        """
        return await manage_watch_impl(
            runtime, await _auth(ctx), watch_id=watch_id, action="delete"
        )
