"""Explicit request-owned Gemini SDK transport, with no ambient routing."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import httpx
from pydantic import JsonValue

from mux.drivers import isolated_ssl_context
from mux.drivers.gemini.catalog_transport import AgentCatalogTransport, SDKAgentCatalogTransport
from mux.drivers.gemini.transport import Object, SDKTransport, Transport

if TYPE_CHECKING:
    from google import genai


_TLS_PROXY_ENV = frozenset(
    {
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSLKEYLOGFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)


def _require_clean_environment() -> None:
    # Iterate names only: Mapping membership/getenv can read a present value.
    # Unknown provider knobs (including mixed-case names) also refuse; names
    # and values may contain secrets, so neither appears in the exception.
    if any(
        name.upper().startswith(("GOOGLE_", "GEMINI_")) or name.upper() in _TLS_PROXY_ENV
        for name in os.environ
    ):
        raise ValueError("Gemini transport refuses ambient SDK/TLS/proxy configuration")


class _OwnedStream(AsyncIterator[Object]):
    def __init__(self, source: AsyncIterator[Object], close: Callable[[], Awaitable[None]]) -> None:
        self._source, self._close, self._closed = source, close, False

    def __aiter__(self) -> _OwnedStream:
        return self

    async def __anext__(self) -> Object:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await anext(self._source)
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._source, "aclose", None)
            if callable(close):
                await cast(Callable[[], Awaitable[None]], close)()
        finally:
            await self._close()


class _ConfiguredTransport:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[genai.Client]:
        _require_clean_environment()
        # Validate request policy before constructor imports or TLS/HTTP allocation.
        from google import genai
        from google.genai.types import HttpOptions, HttpRetryOptions

        context = isolated_ssl_context()
        with httpx.Client(trust_env=False, timeout=60, verify=context, proxy=None) as synchronous:
            async with httpx.AsyncClient(
                trust_env=False, timeout=60, verify=context, proxy=None
            ) as asynchronous:
                client = genai.Client(
                    api_key=self._api_key,
                    enterprise=False,
                    vertexai=False,
                    credentials=None,
                    project=None,
                    location=None,
                    debug_config=genai.client.DebugConfig(
                        client_mode=None, replays_directory=None, replay_id=None
                    ),
                    http_options=HttpOptions(
                        base_url="https://generativelanguage.googleapis.com",
                        api_version="v1beta",
                        httpx_client=synchronous,
                        httpx_async_client=asynchronous,
                        client_args={"trust_env": False, "verify": context, "proxy": None},
                        async_client_args={
                            "trust_env": False,
                            "verify": context,
                            "ssl": context,
                            "proxy": None,
                        },
                        headers={"x-goog-api-key": self._api_key},
                        retry_options=HttpRetryOptions(attempts=1),
                    ),
                )
                try:
                    # The interactions SDK counts retries rather than attempts.
                    # Pin the owned clients before exposing any request boundary.
                    client._nextgen_client.max_retries = 0  # pyright: ignore[reportPrivateUsage]
                    client.aio._nextgen_client.max_retries = 0  # pyright: ignore[reportPrivateUsage]
                    yield client
                finally:
                    try:
                        await client.aio.aclose()
                    finally:
                        client.close()

    async def count_input_tokens(self, request: Mapping[str, JsonValue]) -> int:
        async with self._client() as client:
            return await SDKTransport(client).count_input_tokens(request)

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).create(request)

    async def get(self, interaction_id: str) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).get(interaction_id)

    async def cancel(self, interaction_id: str) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        async with self._client() as client:
            return await SDKTransport(client).download_snapshot(environment_id)

    async def open_stream(
        self,
        interaction_id: str,
        *,
        after: str | None = None,
    ) -> AsyncIterator[Object]:
        lifetime = self._client()
        client = await lifetime.__aenter__()
        try:
            source = await SDKTransport(client).open_stream(interaction_id, after=after)
        except BaseException:
            await lifetime.__aexit__(None, None, None)
            raise

        async def close() -> None:
            await lifetime.__aexit__(None, None, None)

        return _OwnedStream(source, close)


def configured_transport(*, api_key: str) -> Transport:
    if not api_key.strip():
        raise ValueError("explicit Gemini credential is required")
    return _ConfiguredTransport(api_key)


class _ConfiguredCatalogTransport(_ConfiguredTransport):
    async def create_agent(self, definition: Mapping[str, JsonValue]) -> Object:
        async with self._client() as client:
            return await SDKAgentCatalogTransport(client).create_agent(definition)

    async def get_agent(self, id_: str) -> Object:
        async with self._client() as client:
            return await SDKAgentCatalogTransport(client).get_agent(id_)


def configured_catalog_transport(*, api_key: str) -> AgentCatalogTransport:
    if not api_key.strip():
        raise ValueError("explicit Gemini credential is required")
    return _ConfiguredCatalogTransport(api_key)
