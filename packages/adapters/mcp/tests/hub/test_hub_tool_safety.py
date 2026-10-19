"""The registered hub wrappers, over the wire, under the unattended tool gate.

A hub read reports a turn paused on confirmations and answers nothing; the
owner's hub `continue_turn` resumes an agent-chat turn under the current
policy; an attended card is never resumed from the hub.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _agent_chat_safety as safety
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    HubSettings,
    McpSettings,
    Settings,
)
from daimon.core.defaults.loader import DeploymentDefault
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import session_settle_state
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing import AGENT_ID, build_turn_router, ma_agent, ma_session
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, send_events_response
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_TOKEN = "tok"
_SESSION = "ses_hub_safety"


class _Paused:
    """A session paused on `linear/create_issue`; a confirmation ends the turn."""

    def __init__(self, account: str) -> None:
        self.account = account
        self.events: list[dict[str, Any]] = [
            {"id": "evt_user", "type": "user.message", "content": [{"type": "text", "text": "x"}]},
            {
                "id": "evt_tool",
                "type": "agent.mcp_tool_use",
                "mcp_server_name": "linear",
                "name": "create_issue",
                "input": {},
                "evaluated_permission": "ask",
            },
            {
                "id": "evt_pause",
                "type": "session.status_idle",
                "stop_reason": {"type": "requires_action", "event_ids": ["evt_tool"]},
            },
        ]
        self.sent: list[dict[str, Any]] = []

    def model(self) -> dict[str, Any]:
        return ma_session(
            id=_SESSION,
            agent=ma_agent(
                id=AGENT_ID,
                tools=[
                    {
                        "type": "mcp_toolset",
                        "mcp_server_name": "linear",
                        "configs": [],
                        "default_config": {
                            "enabled": True,
                            "permission_policy": {"type": "always_ask"},
                        },
                    }
                ],
                mcp_servers=[{"name": "linear", "type": "url", "url": "https://l.example/mcp"}],
            ),
            status="idle",
            metadata={MA_METADATA_KEY_ACCOUNT: self.account},
        ).model_dump(mode="json")

    def route(self, router: MARouter) -> None:
        router.add(
            "GET",
            rf"/v1/sessions/{_SESSION}$",
            lambda _r, _m: httpx.Response(200, json=self.model()),
        )
        router.add("GET", rf"/v1/sessions/{_SESSION}/events$", self._list)
        router.add("POST", rf"/v1/sessions/{_SESSION}/events$", self._send)

    def _list(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        events = list(self.events)
        types = request.url.params.get_list("types[]")
        if types:
            events = [e for e in events if e["type"] in types]
        if request.url.params.get("order") == "desc":
            events.reverse()
        limit = int(request.url.params.get("limit") or 100)
        return httpx.Response(200, json={"data": events[:limit], "next_page": None})

    def _send(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        for event in json_body(request)["events"]:
            self.sent.append(event)
            self.events.append({**event, "id": f"evt_sent_{len(self.sent)}"})
        self.events.append(
            {"id": "evt_end", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}}
        )
        return send_events_response(data=[])


async def _hub(
    db_session: AsyncSession, engine: AsyncEngine, *, owner_is_caller: bool, recorded: bool
) -> tuple[Any, str, _Paused, async_sessionmaker[AsyncSession]]:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if recorded:
        async with factory.begin() as db:
            await session_settle_state.record_unattended_turn(
                db, managed_session_id=_SESSION, turn_event_id="evt_user", rules_channel_id=None
            )
    paused = _Paused(str(principal.account_id) if owner_is_caller else str(uuid.uuid4()))
    router = MARouter()
    paused.route(router)
    build_turn_router(str(tenant.id), router=router)
    runtime = McpRuntime(
        session_factory=factory,
        client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=None, jwt_secret=None),
            hub=HubSettings(),
            tool_safety=ToolSafetyPolicy(enabled=True),
        ),
        deployment_default=DeploymentDefault(agent_name="test-agent", environment_name="test-env"),
    )
    hub_tenant = HubTenant(
        tenant_id=tenant.id, account_id=principal.account_id, workspace_id="g1", workspace_name="W"
    )
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[hub_tenant])
    auth = StaticTokenVerifier(
        tokens={_TOKEN: {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
    )
    mcp = build_hub_app(platform="discord", runtime=runtime, auth=auth, billing_config=None)
    daimon_id = str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT_ID))
    return (
        mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
        daimon_id,
        paused,
        factory,
    )


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("result", result)
    assert isinstance(payload, dict)
    return payload


async def _drain() -> None:
    workers = list(safety._workers.values())  # pyright: ignore[reportPrivateUsage]
    if workers:
        await asyncio.wait_for(asyncio.gather(*workers), timeout=5)


async def test_hub_get_session_reports_the_pause_and_answers_nothing(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    app, daimon_id, paused, _ = await _hub(
        db_session, db_nullpool_engine, owner_is_caller=True, recorded=True
    )
    result = _payload(
        await call_mcp_tool(
            app,
            token=_TOKEN,
            name="get_session",
            arguments={"daimon_id": daimon_id, "handle": _SESSION},
        )
    )
    info = result.get("structuredContent") or {}
    assert (info.get("status"), info.get("hint")) == ("paused", safety.PAUSED_HINT), result
    await asyncio.sleep(0.05)
    assert paused.sent == []
    assert not safety._workers  # pyright: ignore[reportPrivateUsage]


async def test_hub_continue_turn_resumes_the_owners_agent_chat_turn(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety, "_POLL_S", 0.01)
    app, daimon_id, paused, _ = await _hub(
        db_session, db_nullpool_engine, owner_is_caller=True, recorded=True
    )
    result = _payload(
        await call_mcp_tool(
            app,
            token=_TOKEN,
            name="continue_turn",
            arguments={"daimon_id": daimon_id, "handle": _SESSION},
        )
    )
    assert not result.get("isError"), result
    await _drain()
    assert [(e["type"], e.get("result")) for e in paused.sent] == [
        ("user.tool_confirmation", "deny")
    ]


@pytest.mark.parametrize(
    ("owner_is_caller", "recorded", "refusal"),
    [(False, True, "not found"), (True, False, "approval in the chat")],
    ids=["another-account", "attended-card"],
)
async def test_hub_continue_turn_never_answers_what_it_may_not(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    owner_is_caller: bool,
    recorded: bool,
    refusal: str,
) -> None:
    app, daimon_id, paused, _ = await _hub(
        db_session, db_nullpool_engine, owner_is_caller=owner_is_caller, recorded=recorded
    )
    result = _payload(
        await call_mcp_tool(
            app,
            token=_TOKEN,
            name="continue_turn",
            arguments={"daimon_id": daimon_id, "handle": _SESSION, "message": "go on"},
        )
    )
    assert result.get("isError") and refusal in str(result.get("content")), result
    await asyncio.sleep(0.05)
    assert paused.sent == []
