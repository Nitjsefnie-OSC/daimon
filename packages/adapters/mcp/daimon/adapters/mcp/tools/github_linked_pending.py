"""Resume a one-use chat grant after the requester verifies a GitHub link."""

from __future__ import annotations

import uuid
from typing import Literal

import discord
import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.github_linked_grant import linked_grant
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.github_panel import requester_manages_agent
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.github_linked_pending import PendingGrant, save, take
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


async def save_pending(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    ma_agent_id: str,
    agent_name: str,
    repo_name: str,
    requested_access: Literal["read", "write"] = "read",
    platform: str,
    platform_user_id: str,
    workspace_id: str,
    parent_channel_id: str,
    thread_id: str,
) -> None:
    await save(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        ma_agent_id=ma_agent_id,
        agent_name=agent_name,
        repo_name=repo_name,
        requested_access=requested_access,
        platform=platform,
        platform_user_id=platform_user_id,
        workspace_id=workspace_id,
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
    )


async def _take_pending(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> list[PendingGrant]:
    async with runtime.session_factory.begin() as session:
        rows = await take(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            platform_user_id=platform_user_id,
        )
        return rows


async def _deliver_result(runtime: McpRuntime, pending: PendingGrant, message: str) -> None:
    if pending.platform == "discord":
        async with rest_client(_require_bot_token(runtime)) as client:
            channel = await client.fetch_channel(int(pending.thread_id))
            if not isinstance(channel, discord.Thread):
                raise ValueError("The originating GitHub grant thread is unavailable.")
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    elif pending.platform == "slack":
        client = await slack_web_client(runtime, team_id=pending.workspace_id)
        await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=pending.parent_channel_id,
            thread_ts=pending.thread_id,
            text=message,
        )


async def resume_pending(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> list[str]:
    pending = await _take_pending(
        runtime,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        platform_user_id=platform_user_id,
    )
    results: list[str] = []
    for row in pending:
        agent = await find_agent_by_daimon_tag(
            runtime.client, tenant_id=tenant_id, name=row.agent_name
        )
        if (
            agent is None
            or str(agent.id) != row.ma_agent_id
            or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)) != row.agent_id
        ):
            message = "Agent access changed. Ask again in chat."
        else:

            async def manages_now(
                session: AsyncSession,
                *,
                agent: BetaManagedAgentsAgent = agent,
                row: PendingGrant = row,
            ) -> bool:
                account = await get_account(session, account_id)
                if account is None or account.tenant_id != tenant_id or account.is_external:
                    return False
                return await requester_manages_agent(
                    session,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    platform=platform,
                    platform_user_id=platform_user_id,
                    agent_name=agent.name,
                    other_names=(
                        row.agent_name,
                        str(agent.metadata.get(MA_METADATA_KEY_NAME) or ""),
                    ),
                    ma_agent_id=str(agent.id),
                    default=runtime.deployment_default,
                    is_daimon_managed=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
                    members=(
                        runtime.group_lookups.live_members(platform, row.workspace_id)
                        if runtime.group_lookups is not None
                        else None
                    ),
                )

            async with runtime.session_factory() as session:
                allowed = await manages_now(session)
            if not allowed:
                message = "Agent access changed. Ask again in chat."
            else:
                config = runtime.settings.github_app
                try:
                    outcome = await linked_grant(
                        runtime.session_factory,
                        tenant_id=tenant_id,
                        account_id=account_id,
                        agent_id=row.agent_id,
                        agent_name=agent.name,
                        repo_name=row.repo_name,
                        fernet=runtime.fernet,
                        client_id=config.client_id or "",
                        client_secret=config.client_secret.get_secret_value()
                        if config.client_secret
                        else "",
                        app_id=config.app_id or "",
                        private_key=config.private_key.get_secret_value()
                        if config.private_key
                        else "",
                        manages_now=manages_now,
                        requested_access=row.requested_access,
                    )
                    message = outcome.message
                except DBAPIError as exc:
                    if getattr(exc.orig, "sqlstate", None) not in ("40001", "23505"):
                        raise
                    message = "Access changed. Ask again in chat."
                except httpx.HTTPError:
                    message = "GitHub didn't answer. Ask again in chat."
        await _deliver_result(runtime, row, message)
        results.append(message)
    return results
