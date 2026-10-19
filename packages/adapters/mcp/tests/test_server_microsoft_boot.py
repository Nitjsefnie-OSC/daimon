"""create_mcp_app with Microsoft accounts unset, partly set and fully set.

Unset, nothing about Microsoft shows up or is logged. Partly set, the feature
stays off and one warning names what is missing; the server still boots.
"""

from __future__ import annotations

import pytest
import structlog
from cryptography.fernet import Fernet
from daimon.adapters.mcp import server as server_module
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    McpSettings,
    MicrosoftAccountsSettings,
    Settings,
)
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.routing import Route
from structlog.testing import CapturingLogger

APP = MicrosoftAccountsSettings(
    client_id="app-1",
    client_secret=SecretStr("app-secret"),
    tenant_id="00000000-0000-4000-8000-0000000000aa",
)
KEYS = CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),))


def _settings(microsoft: MicrosoftAccountsSettings, crypto: CryptoSettings) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        microsoft_accounts=microsoft,
        crypto=crypto,
    )


@pytest.mark.parametrize(
    ("microsoft", "crypto", "on", "missing"),
    [
        (MicrosoftAccountsSettings(), KEYS, False, None),
        (APP, CryptoSettings(), False, ["DAIMON_CRYPTO__KEYS"]),
        (
            MicrosoftAccountsSettings(client_id="app-1"),
            KEYS,
            False,
            ["DAIMON_MICROSOFT_ACCOUNTS__CLIENT_SECRET", "DAIMON_MICROSOFT_ACCOUNTS__TENANT_ID"],
        ),
        (APP, KEYS, True, None),
    ],
    ids=["unset", "no crypto keys", "client id only", "complete"],
)
async def test_microsoft_tools_routes_and_warnings_follow_the_setup(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    microsoft: MicrosoftAccountsSettings,
    crypto: CryptoSettings,
    on: bool,
    missing: list[str] | None,
) -> None:
    # create_mcp_app configures structlog itself, so its module logger is swapped.
    captured = CapturingLogger()
    monkeypatch.setattr(server_module, "log", structlog.wrap_logger(captured, processors=[]))
    app = create_mcp_app(
        settings=_settings(microsoft, crypto),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    tools = {tool.name for tool in await app.state.mcp.local_provider.list_tools()}
    routes = {r.path for r in app.routes if isinstance(r, Route)}
    microsoft_tools = {name for name in tools if "microsoft" in name}
    assert bool(microsoft_tools) is on, f"Microsoft tools must be offered only when set up: {tools}"
    assert ("/oauth/microsoft/callback" in routes) is on, "the sign-in routes follow the tools"
    warnings = [c for c in captured.calls if "microsoft" in str(c.kwargs.get("event"))]
    if missing is None:
        assert not warnings, f"an unused or complete setup must log nothing: {warnings}"
    else:
        assert [(w.method_name, w.kwargs["missing"]) for w in warnings] == [("warning", missing)], (
            "a partial setup must warn once, naming what is missing"
        )
