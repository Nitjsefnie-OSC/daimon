"""Resume a one-use chat grant after the requester verifies a GitHub link."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import discord
import httpx
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import _require_bot_token, rest_client
from daimon.adapters.mcp.tools.github_linked_grant import linked_grant
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core._models import Account, GitHubLinkedConnectPending
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.github_panel import requester_manages_agent
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.domain import Role
from sqlalchemy import delete, select
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
    platform: str,
    platform_user_id: str,
    workspace_id: str,
    parent_channel_id: str,
    thread_id: str,
) -> None:
    session.add(
        GitHubLinkedConnectPending(
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            ma_agent_id=ma_agent_id,
            agent_name=agent_name,
            repo_name=repo_name,
            platform=platform,
            platform_user_id=platform_user_id,
            workspace_id=workspace_id,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await session.flush()


async def _take_pending(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> list[GitHubLinkedConnectPending]:
    async with runtime.session_factory.begin() as session:
        rows = (
            await session.scalars(
                select(GitHubLinkedConnectPending)
                .where(
                    GitHubLinkedConnectPending.tenant_id == tenant_id,
                    GitHubLinkedConnectPending.account_id == account_id,
                    GitHubLinkedConnectPending.platform == platform,
                    GitHubLinkedConnectPending.platform_user_id == platform_user_id,
                    GitHubLinkedConnectPending.expires_at > datetime.now(UTC),
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            await session.delete(row)
        await session.execute(
            delete(GitHubLinkedConnectPending).where(
                GitHubLinkedConnectPending.expires_at <= datetime.now(UTC)
            )
        )
        return rows


async def _deliver_result(
    runtime: McpRuntime, pending: GitHubLinkedConnectPending, message: str
) -> None:
    if pending.platform == "discord":
        async with rest_client(_require_bot_token(runtime)) as client:
            user = await client.fetch_user(int(pending.platform_user_id))
            await user.send(message, allowed_mentions=discord.AllowedMentions.none())
    elif pending.platform == "slack":
        client = await slack_web_client(runtime, team_id=pending.workspace_id)
        await client.chat_postEphemeral(
            channel=pending.parent_channel_id,
            thread_ts=pending.thread_id,
            user=pending.platform_user_id,
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

            async def manages_now(session: AsyncSession, *, agent=agent, row=row) -> bool:
                account = await session.get(Account, account_id)
                if account is None or account.tenant_id != tenant_id or account.is_external:
                    return False
                if account.role == Role.ADMIN:
                    return True
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
