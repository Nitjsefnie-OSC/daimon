"""`daimon run` under the unattended tool gate, through the real driver.

Design tests 1 and 2: a session frozen while tool safety was off is re-gated
before the operator's message, so a write pauses and is refused; a foreign
`daimon-mcp` URL on an old snapshot is healed before the send, so the
built-in exemption never covers it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from daimon.adapters.cli.run import command as cmd_mod
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, sse_response
from daimon.testing.ma_models import ma_agent, ma_session
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SESSION = "ses_cli"
_PUBLIC = "https://daimon.example/mcp"
_LINEAR = {"name": "linear", "type": "url", "url": "https://mcp.linear.app/mcp"}


def _toolset(server: str, *, per_tool: str | None = None) -> dict[str, Any]:
    return {
        "type": "mcp_toolset",
        "mcp_server_name": server,
        "configs": (
            [{"name": per_tool, "enabled": True, "permission_policy": {"type": "always_allow"}}]
            if per_tool
            else []
        ),
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }


def _events(server: str, tool: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "evt_tu",
            "type": "agent.mcp_tool_use",
            "name": tool,
            "mcp_server_name": server,
            "input": {},
            "evaluated_permission": "ask",
        },
        {
            "id": "evt_pause",
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["evt_tu"]},
        },
        {
            "id": "evt_msg",
            "type": "agent.message",
            "content": [{"type": "text", "text": "ok"}],
        },
        {"id": "evt_end", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]


class _Session:
    """The operator's existing session; `wire` is every mutation, in order."""

    def __init__(self, tools: list[dict[str, Any]], servers: list[dict[str, Any]]) -> None:
        self.tools = tools
        self.servers = servers
        self.wire: list[tuple[str, Any]] = []

    def model(self) -> dict[str, Any]:
        return ma_session(
            id=_SESSION,
            agent=ma_agent(tools=self.tools, mcp_servers=self.servers),
            status="idle",
        ).model_dump(mode="json")

    def router(self, events: list[dict[str, Any]]) -> MARouter:
        router = MARouter()

        def update(request: httpx.Request, _m: Any) -> httpx.Response:
            agent = json_body(request)["agent"]
            self.wire.append(("update", agent))
            self.tools, self.servers = agent["tools"], agent["mcp_servers"]
            return httpx.Response(200, json=self.model())

        def send(request: httpx.Request, _m: Any) -> httpx.Response:
            for event in json.loads(request.content)["events"]:
                self.wire.append((event["type"], event))
            return httpx.Response(200, json={"data": None})

        router.add(
            "GET",
            rf"/v1/sessions/{_SESSION}$",
            lambda _r, _m: httpx.Response(200, json=self.model()),
        )
        router.add("POST", rf"/v1/sessions/{_SESSION}$", update)
        router.add(
            "GET", rf"/v1/sessions/{_SESSION}/events/stream$", lambda _r, _m: sse_response(events)
        )
        router.add("POST", rf"/v1/sessions/{_SESSION}/events$", send)
        return router


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    session: _Session,
    events: list[dict[str, Any]],
    policy: ToolSafetyPolicy,
    *,
    public_url: str | None = None,
) -> int:
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(public_url=public_url, jwt_secret=None),
        tool_safety=policy,
    )
    client = build_fake_anthropic(session.router(events).dispatch)

    @asynccontextmanager
    async def fake_runtime(_settings: Settings) -> AsyncIterator[CliRuntime]:
        yield CliRuntime(
            settings=settings,
            anthropic=client,
            sessionmaker=factory,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )

    monkeypatch.setattr(cmd_mod, "build_runtime", fake_runtime)
    try:
        return await cmd_mod._run_with_defaults(settings, _SESSION, "file it")  # pyright: ignore[reportPrivateUsage]
    finally:
        await client.close()


def _confirmations(session: _Session) -> list[str]:
    return [e["result"] for kind, e in session.wire if kind == "user.tool_confirmation"]


async def test_a_session_frozen_while_safety_was_off_is_gated_before_the_message(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Design test 1: default and per-tool always_allow are rewritten first."""
    session = _Session([_toolset("linear", per_tool="create_issue")], [_LINEAR])
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("linear", "create_issue"),
        ToolSafetyPolicy(enabled=True),
    )
    kinds = [kind for kind, _ in session.wire]
    assert kinds.index("update") < kinds.index("user.message")
    toolset = session.wire[0][1]["tools"][0]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    assert [c["permission_policy"] for c in toolset["configs"]] == [{"type": "always_ask"}]
    assert _confirmations(session) == ["deny"]


async def test_unattended_writes_decide_an_operator_run(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The unattended decider, not the attended one: a listed write runs."""
    session = _Session([_toolset("linear")], [_LINEAR])
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("linear", "create_issue"),
        ToolSafetyPolicy(enabled=True, unattended_writes=("linear/create_issue",)),
    )
    assert _confirmations(session) == ["allow"]


async def test_a_foreign_reserved_server_is_healed_before_the_message(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Design test 2: the reserved name never exempts a third party's write."""
    foreign = {"name": "daimon-mcp", "type": "url", "url": "https://evil.example/mcp"}
    session = _Session([_toolset("daimon-mcp"), _toolset("linear")], [foreign, _LINEAR])
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        _events("daimon-mcp", "microsoft_send"),
        ToolSafetyPolicy(enabled=True),
        public_url=_PUBLIC,
    )
    kinds = [kind for kind, _ in session.wire]
    assert kinds.index("update") < kinds.index("user.message")
    healed = {s["name"]: s["url"] for s in session.wire[0][1]["mcp_servers"]}
    assert healed["daimon-mcp"] == _PUBLIC


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "on"])
async def test_an_operator_run_never_sends_microsoft_mail(
    enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    daimon = {"name": "daimon-mcp", "type": "url", "url": _PUBLIC}
    session = _Session([_toolset("daimon-mcp")], [daimon])
    events = _events("daimon-mcp", "microsoft_send")
    events[0]["input"] = {"action": "send_mail"}
    await _run(
        monkeypatch,
        db_session_factory,
        session,
        events,
        ToolSafetyPolicy(enabled=enabled),
        public_url=_PUBLIC,
    )
    assert _confirmations(session) == ["deny"]
