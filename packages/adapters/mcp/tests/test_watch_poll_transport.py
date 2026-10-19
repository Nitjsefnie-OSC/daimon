"""The private executor accepts lease identifiers, never caller-selected source or identity."""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.adapters.mcp.watch_polling import poll_endpoint
from starlette.applications import Starlette
from starlette.routing import Route


@pytest.mark.parametrize("enabled", [True, False])
async def test_executor_ignores_forged_body_and_respects_producer_gate(monkeypatch, enabled):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", str(enabled).lower())
    run = AsyncMock(return_value=True)
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.run_poll", run)
    runtime = object()
    watch_id, lease = uuid.uuid4(), str(uuid.uuid4())
    app = Starlette(
        routes=[
            Route("/internal/watches/{watch_id}/poll", poll_endpoint(runtime), methods=["POST"])
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://os"
    ) as client:
        response = await client.post(
            f"/internal/watches/{watch_id}/poll",
            headers={"X-Watch-Lease": lease},
            json={
                "owner": "evil",
                "tool": "http_get",
                "arguments": {"url": "https://outside"},
                "instructions": "write",
                "payload": "forged",
            },
        )
    assert response.status_code == (204 if enabled else 503)
    if enabled:
        run.assert_awaited_once_with(runtime, watch_id, lease)
    else:
        run.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-Watch-Lease", "invalid")],
        [("X-Watch-Lease", str(uuid.uuid4())), ("X-Watch-Lease", str(uuid.uuid4()))],
    ],
)
async def test_executor_refuses_missing_malformed_or_duplicate_leases(monkeypatch, headers):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    run = AsyncMock()
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.run_poll", run)
    app = Starlette(
        routes=[
            Route("/internal/watches/{watch_id}/poll", poll_endpoint(object()), methods=["POST"])
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://os"
    ) as client:
        response = await client.post(f"/internal/watches/{uuid.uuid4()}/poll", headers=headers)
    assert response.status_code == 403
    run.assert_not_called()


async def test_executor_bounds_invalid_lease_work_before_database_calls(monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    run = AsyncMock(return_value=False)
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.run_poll", run)
    app = Starlette(
        routes=[
            Route("/internal/watches/{watch_id}/poll", poll_endpoint(object()), methods=["POST"])
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://os"
    ) as client:
        for _ in range(120):
            response = await client.post(
                f"/internal/watches/{uuid.uuid4()}/poll",
                headers={"X-Watch-Lease": str(uuid.uuid4())},
            )
            assert response.status_code == 403
        response = await client.post(
            f"/internal/watches/{uuid.uuid4()}/poll", headers={"X-Watch-Lease": str(uuid.uuid4())}
        )
        assert response.status_code == 429
    assert run.await_count == 120


async def test_lease_executor_is_mounted_on_real_os_app_without_owner_credentials(
    monkeypatch, sessionmaker
):
    from daimon.adapters.mcp.server import create_mcp_app
    from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
    from daimon.testing.ma import build_stub_anthropic
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from pydantic import HttpUrl, PostgresDsn, SecretStr

    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    run = AsyncMock(return_value=True)
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.run_poll", run)
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            mcp=McpSettings(
                jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://os.example/mcp")
            ),
        ),
        sessionmaker=sessionmaker,
        anthropic=build_stub_anthropic(lambda _: httpx.Response(404)),
        auth=StaticTokenVerifier(tokens={}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://os"
    ) as client:
        result = await client.post(
            f"/internal/watches/{uuid.uuid4()}/poll", headers={"X-Watch-Lease": str(uuid.uuid4())}
        )
    assert result.status_code == 204
    run.assert_awaited_once()
