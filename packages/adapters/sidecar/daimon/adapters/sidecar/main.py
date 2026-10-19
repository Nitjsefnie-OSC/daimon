"""Capability-only ingress. No turn runner, credential reader or provider client."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from aiohttp import web
from daimon.adapters.sidecar.settings import SidecarSettings
from daimon.core.stores.watches import accept_callback
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

MAX_PAYLOAD = 16384


def create_app(
    sessions: async_sessionmaker[AsyncSession],
    *,
    max_concurrent: int = 8,
    request_timeout_s: float = 10,
) -> web.Application:
    """Only callback POST paths accept input; health is private at the proxy."""
    app = web.Application(client_max_size=MAX_PAYLOAD)
    active = 0

    async def health(_: web.Request) -> web.Response:
        try:
            async with asyncio.timeout(2), sessions() as session:
                await session.execute(text("SELECT 1"))
            return web.json_response({"status": "ok"})
        except (TimeoutError, DBAPIError):
            return web.json_response({"status": "unavailable"}, status=503)

    async def callback(request: web.Request) -> web.Response:
        nonlocal active
        # No wait queue: requests awaiting body reads also consume a slot.
        if active >= max_concurrent:
            return web.Response(status=429)
        active += 1
        try:
            async with asyncio.timeout(request_timeout_s):
                try:
                    watch_id = uuid.UUID(request.match_info["watch_id"])
                except ValueError:
                    return web.Response(status=404)
                auth = request.headers.get("Authorization", "")
                event_id = request.headers.get("X-Event-ID", "")
                if not auth.startswith("Bearer ") or not 1 <= len(event_id) <= 128:
                    return web.Response(status=403)
                # Streaming bounds work without Content-Length, including chunked senders.
                body = bytearray()
                async for chunk in request.content.iter_chunked(4096):
                    body.extend(chunk)
                    if len(body) > MAX_PAYLOAD:
                        return web.Response(status=413)
                try:
                    payload = body.decode("utf-8")
                except UnicodeDecodeError:
                    return web.Response(status=400)
                async with sessions.begin() as session:
                    key = await accept_callback(
                        session,
                        watch_id=watch_id,
                        capability=auth[7:],
                        event_id=event_id,
                        payload=payload,
                    )
                # The context commits before success is observable by the sender.
                return web.json_response({"accepted": True, "wake_id": str(key)}, status=202)
        except DBAPIError as exc:
            # Never log exception text/parameters, authorization headers or bodies.
            state = getattr(exc.orig, "sqlstate", None)
            return web.Response(
                status=429 if state == "54000" else 403 if state in ("42501", "22023") else 503
            )
        except TimeoutError:
            return web.Response(status=503)
        finally:
            active -= 1

    app.router.add_get("/healthz", health)
    app.router.add_post("/callbacks/watches/{watch_id}", callback)
    return app


def run() -> None:
    settings = SidecarSettings()  # pyright: ignore[reportCallIssue]
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=2,
        max_overflow=2,
        pool_timeout=2,
        hide_parameters=True,
        connect_args={"server_settings": {"statement_timeout": "8000", "lock_timeout": "3000"}},
    )
    app = create_app(
        async_sessionmaker(engine, expire_on_commit=False),
        max_concurrent=settings.max_concurrent,
        request_timeout_s=settings.request_timeout_s,
    )

    async def cleanup(_: web.Application) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app.cleanup_ctx.append(cleanup)
    # Access logs are deliberately disabled: capabilities belong in headers only.
    web.run_app(
        app,
        host=settings.host,
        port=settings.port,
        access_log=None,
        handler_args={"request_timeout": settings.request_timeout_s},
    )
