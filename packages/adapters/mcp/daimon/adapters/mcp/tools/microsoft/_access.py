"""A Teams person's Microsoft Graph access, from their stored refresh token.

Each call reads the stored row, so a disconnect or reconnect from another
process applies at once. Access tokens are cached in memory against the
connection (`created_at`), which a refresh's rotation leaves alone; a refresh
stores the rotated refresh token over the row it came from, and losing that
race to another process keeps the stored token. A revoked grant deletes the
row and asks the person to reconnect.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import structlog
from cachetools import TTLCache
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.config import MicrosoftAccountsSettings
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.microsoft_identity import (
    MicrosoftAppRejected,
    MicrosoftGrantRevoked,
    MicrosoftIdentityError,
    refresh_tokens,
)
from daimon.core.stores import microsoft_user_tokens as tokens_store
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
# Neither tells the agent to connect on its own: a sign-in card nobody asked
# for is unsolicited, so the person is asked first.
NOT_CONNECTED = (
    "This person has not connected their Microsoft account, so nothing was read or "
    "changed. Tell them, and ask whether they want to connect it; call "
    "connect_microsoft only if they say yes."
)
_REVOKED = (
    "This person's Microsoft connection has expired or was revoked, so it was removed. "
    "Tell them, and call connect_microsoft only if they want to connect again."
)
_APP_REJECTED = (
    "Microsoft refused this server's app registration ({code}), so nothing was read or "
    "changed and the person's connection was kept. Tell them an administrator needs to "
    "fix the Microsoft 365 settings; do not retry or reconnect."
)
_UNREADABLE = (
    "This person's saved Microsoft sign-in cannot be read with this server's keys, so "
    "nothing was read or changed. Tell them, and call connect_microsoft only if they want "
    "to connect again."
)
_UNAVAILABLE = (
    "Microsoft sign-in did not answer ({code}), so nothing was read or changed. Try once "
    "more in a minute at most, then tell the person it is unavailable."
)
_log = structlog.get_logger(__name__)
_EARLY = 120.0  # seconds before expiry an access token is refreshed

HttpFactory = Callable[[], httpx.AsyncClient]


@dataclass(frozen=True, slots=True)
class _Cached:
    access_token: str
    expires_at: float
    connected_at: datetime


@dataclass
class MicrosoftGraph:
    config: MicrosoftAccountsSettings
    fernet: MultiFernet
    session_factory: async_sessionmaker[AsyncSession]
    http: HttpFactory = lambda: httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    clock: Callable[[], float] = time.monotonic
    _cache: TTLCache[tuple[uuid.UUID, str], _Cached] = field(
        default_factory=lambda: TTLCache[tuple[uuid.UUID, str], _Cached](maxsize=2048, ttl=3600)
    )
    _locks: dict[tuple[uuid.UUID, str], asyncio.Lock] = field(
        default_factory=dict[tuple[uuid.UUID, str], asyncio.Lock]
    )

    async def access_token(
        self, tenant_id: uuid.UUID, user_id: str, *, stale: str | None = None
    ) -> str:
        """A live access token; `stale` is one Graph just refused, forcing a refresh."""
        key = (tenant_id, user_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            async with self.session_factory() as session:
                row = await tokens_store.get_microsoft_user_token(
                    session, tenant_id=tenant_id, user_id=user_id
                )
            if row is None:
                self._cache.pop(key, None)
                raise ToolError(NOT_CONNECTED)
            cached = self._cache.get(key)
            if (
                cached is not None
                and cached.connected_at == row.created_at
                and cached.access_token != stale
                and cached.expires_at - _EARLY > self.clock()
            ):
                return cached.access_token
            try:
                refresh_token = decrypt_token(self.fernet, row.encrypted_refresh_token)
            except InvalidToken:
                # Kept: the keys may be fixed, and reconnecting overwrites it anyway.
                _log.warning("microsoft_accounts.token_unreadable")
                raise ToolError(_UNREADABLE) from None
            try:
                async with self.http() as http:
                    tokens = await refresh_tokens(http, self.config, refresh_token=refresh_token)
            except MicrosoftGrantRevoked:
                self._cache.pop(key, None)
                async with self.session_factory.begin() as session:
                    await tokens_store.delete_microsoft_user_token(
                        session, tenant_id=tenant_id, user_id=user_id, read_at=row.updated_at
                    )
                raise ToolError(_REVOKED) from None
            except MicrosoftAppRejected as exc:
                _log.error("microsoft_accounts.app_rejected", code=str(exc))
                raise ToolError(_APP_REJECTED.format(code=exc)) from None
            except (MicrosoftIdentityError, httpx.HTTPError) as exc:
                # Outages and throttling never delete the saved connection.
                code = str(exc) if isinstance(exc, MicrosoftIdentityError) else type(exc).__name__
                raise ToolError(_UNAVAILABLE.format(code=code)) from None
            connected_at = row.created_at
            if tokens.refresh_token and tokens.refresh_token != refresh_token:
                async with self.session_factory.begin() as session:
                    stored = await tokens_store.update_microsoft_refresh_token(
                        session,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        read_at=row.updated_at,
                        encrypted_refresh_token=encrypt_token(self.fernet, tokens.refresh_token),
                        scopes=tokens.scope or row.scopes,
                        now=datetime.now(UTC),
                    )
                if stored is None:
                    # Another process rotated it first (its token stays stored), or
                    # the person disconnected or reconnected meanwhile.
                    async with self.session_factory() as session:
                        current = await tokens_store.get_microsoft_user_token(
                            session, tenant_id=tenant_id, user_id=user_id
                        )
                    if current is None:
                        self._cache.pop(key, None)
                        raise ToolError(NOT_CONNECTED)
                    if current.created_at != connected_at:
                        return tokens.access_token  # the old grant's token: never cached
            self._cache[key] = _Cached(
                access_token=tokens.access_token,
                expires_at=self.clock() + tokens.expires_in,
                connected_at=connected_at,
            )
            return tokens.access_token

    async def request(
        self,
        tenant_id: uuid.UUID,
        user_id: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: object = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One Graph call as the person; a 401 refreshes the token and retries once."""

        async def send(token: str) -> httpx.Response:
            async with self.http() as http:
                try:
                    return await http.request(
                        method,
                        GRAPH_ROOT + path,
                        params=params,
                        json=json,
                        content=content,
                        headers={**(headers or {}), "Authorization": f"Bearer {token}"},
                    )
                except httpx.HTTPError as exc:
                    raise ToolError(
                        f"Microsoft Graph did not answer ({type(exc).__name__}); try again."
                    ) from exc

        token = await self.access_token(tenant_id, user_id)
        response = await send(token)
        if response.status_code == 401:
            response = await send(await self.access_token(tenant_id, user_id, stale=token))
        return response
