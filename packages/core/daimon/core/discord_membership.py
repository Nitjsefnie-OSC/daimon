"""Discord REST membership transport for gateway, scheduler and MCP hosts.

Uses the existing bot credential. No gateway/adapter imports. Both the member
and guild role permissions must resolve: stored account roles are never admin
proof. The resolver owns caching; transport retries rate limits only within
its ten-second lookup deadline. Inject an httpx transport for offline tests.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import cast
from uuid import UUID

import httpx
from daimon.core.membership import CachedMembershipResolver, Membership


class DiscordRestMembershipResolver(CachedMembershipResolver):
    def __init__(
        self,
        bot_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._bot_token = bot_token
        self._transport = transport
        super().__init__(self._fetch_member, clock=clock)

    async def _get(self, client: httpx.AsyncClient, path: str) -> httpx.Response:
        for _ in range(3):
            response = await client.get(path)
            if response.status_code != 429:
                return response
            retry: object = response.json().get("retry_after")
            if not isinstance(retry, int | float) or not 0 <= retry <= 3:
                raise ValueError("unbounded Discord rate limit")
            await asyncio.sleep(retry)
        raise ValueError("Discord rate limit exhausted")

    async def _fetch_member(self, tenant_id: UUID, guild_id: str, user_id: str) -> Membership:
        del tenant_id  # Tenant binding is enforced by the caller and cache key.
        if not guild_id.isdecimal() or not user_id.isdecimal():
            return Membership()
        async with httpx.AsyncClient(
            base_url="https://discord.com/api/v10/",
            headers={"Authorization": f"Bot {self._bot_token}"},
            timeout=5,
            transport=self._transport,
        ) as client:
            member = await self._get(client, f"guilds/{guild_id}/members/{user_id}")
            if member.status_code == 404:
                return Membership(is_member=False)
            member.raise_for_status()
            data = cast("dict[str, object]", member.json())
            raw_roles = data.get("roles")
            if not isinstance(raw_roles, list):
                return Membership()
            values = cast("list[object]", raw_roles)
            if not all(isinstance(r, str) for r in values):
                return Membership()
            roles = tuple(r for r in cast("list[str]", raw_roles) if r != guild_id)
            guild = await self._get(client, f"guilds/{guild_id}")
            guild.raise_for_status()
            role_response = await self._get(client, f"guilds/{guild_id}/roles")
            role_response.raise_for_status()
            permissions = 0
            for role in cast("list[dict[str, str]]", role_response.json()):
                if role["id"] in (*roles, guild_id):
                    permissions |= int(role["permissions"])
            return Membership(
                is_member=True,
                role_ids=roles,
                is_admin=guild.json().get("owner_id") == user_id or bool(permissions & (8 | 32)),
            )
