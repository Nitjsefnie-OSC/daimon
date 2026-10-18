"""Offline contracts for the agent Teams verbs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.graph import GraphTeamsDriver
from qa.live.teams.harness import parse_since, read, send, wait
from qa.live.teams.tokens import ReauthRequired, Secret, TokenStore


def message(mid: str, when: datetime, *, bot: bool = False, text: str = "hello") -> dict[str, Any]:
    sender = {"application": {"id": "bot-app"}} if bot else {"user": {"id": "admin-oid"}}
    return {
        "id": mid,
        "createdDateTime": when.isoformat(),
        "from": sender,
        "body": {"content": f"<p>{text}</p>"},
    }


class FakeDriver:
    def __init__(self, config: TeamsQaConfig) -> None:
        self.config = config
        self.clock = 0.0
        self.items: list[dict[str, Any]] = []
        self.posts = 0
        self.verified = 0
        self.failure: Exception | None = None

    def now(self) -> float:
        return self.clock

    async def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def require_distinct_personas(self) -> dict[str, str]:
        if self.failure:
            raise self.failure
        return {"admin": "admin-oid", "member": "member-oid"}

    async def ensure_target_verified(self, persona: str) -> None:
        self.verified += 1

    async def bot_chat(self, persona: str) -> tuple[str, list[dict[str, Any]]]:
        return "bot-chat", self.items

    async def chat_messages(
        self, persona: str, chat_id: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        return self.items

    async def channel_messages(
        self, persona: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        return self.items

    async def read_replies(
        self, persona: str, message_id: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        return self.items

    async def channel_message(
        self, persona: str, message_id: str, deadline: float | None = None
    ) -> dict[str, Any]:
        return message(message_id, datetime.now(UTC) - timedelta(seconds=10))

    async def post_chat_command(
        self, persona: str, chat_id: str, command: str, snapshot: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.posts += 1
        return message("sent", datetime.now(UTC))

    async def post_mention(self, persona: str, text: str, correlation_id: str) -> dict[str, Any]:
        self.posts += 1
        return message("sent", datetime.now(UTC))


@pytest.fixture
def config(tmp_path: Path) -> TeamsQaConfig:
    source = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    source["token_dir"] = str(tmp_path / "tokens")
    source["bot_app_id"] = "bot-app"
    return TeamsQaConfig.model_validate(source)


def fake(config: TeamsQaConfig) -> tuple[FakeDriver, GraphTeamsDriver]:
    driver = FakeDriver(config)
    return driver, cast(GraphTeamsDriver, driver)


@pytest.mark.asyncio
async def test_send_quiet_chat_and_reauth(config: TeamsQaConfig) -> None:
    driver, graph = fake(config)
    assert (await send(graph, "admin", "chat", "routines"))["message_id"] == "sent"
    assert driver.posts == driver.verified == 1
    driver.items = [message("pending", datetime.now(UTC))]
    assert (await send(graph, "admin", "chat", "routines"))["error"] == "chat_not_quiet"
    assert driver.posts == 1
    driver.failure = ReauthRequired("admin", "expired")
    assert await send(graph, "admin", "chat", "routines") == {
        "ok": False,
        "error": "reauth_required",
        "persona": "admin",
    }
    assert (await send(graph, "missing", "chat", "hi"))["error"] == "unknown_persona"


@pytest.mark.asyncio
async def test_read_filters_cards_since_and_thread(config: TeamsQaConfig) -> None:
    driver, graph = fake(config)
    recent = message(
        "bot", datetime.now(UTC) - timedelta(seconds=2), bot=True, text="<b>answer</b>"
    )
    recent["attachments"] = [
        {
            "content": {
                "body": [{"type": "TextBlock", "text": "Routines"}],
                "actions": [{"type": "Action.Submit", "title": "Refresh"}],
            }
        }
    ]
    driver.items = [message("user", datetime.now(UTC)), recent]
    result = await read(graph, "admin", "thread:parent", "5m")
    assert result["messages"] == [
        {
            "id": "bot",
            "created": recent["createdDateTime"],
            "text": "answer",
            "card_texts": ["Routines"],
            "action_titles": ["Refresh"],
        }
    ]
    json.dumps(result)
    assert parse_since("24h") == timedelta(hours=24)
    assert (await read(graph, "admin", "channel", "25h"))["error"] == "since_exceeds_24h"


@pytest.mark.asyncio
async def test_wait_quiet_window_and_timeout(config: TeamsQaConfig) -> None:
    driver, graph = fake(config)
    now = datetime.now(UTC)
    driver.items = [message("start", now - timedelta(seconds=10)), message("reply", now, bot=True)]
    result = await wait(graph, "admin", "start", 10)
    assert result["ok"] is True and result["messages"][0]["id"] == "reply"
    assert driver.clock >= 3
    driver.items = [message("start", now)]
    assert await wait(graph, "admin", "start", 2) == {
        "ok": False,
        "error": "timeout",
        "after": "start",
    }


@pytest.mark.asyncio
async def test_wait_thread_uses_parent_server_time(config: TeamsQaConfig) -> None:
    driver, graph = fake(config)
    driver.items = [message("reply", datetime.now(UTC), bot=True)]
    result = await wait(graph, "admin", "parent", 5, "thread:parent")
    assert result["ok"] is True
    assert result["messages"][0]["id"] == "reply"
    json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [503, "transport"])
async def test_channel_post_once_and_mention(config: TeamsQaConfig, failure: int | str) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            if request.url.path.endswith("/channels/" + config.channel_id):
                return httpx.Response(
                    200, json={"id": config.channel_id, "displayName": config.channel_display_name}
                )
            if request.url.path.endswith("/teams/" + config.team_id):
                return httpx.Response(
                    200, json={"id": config.team_id, "displayName": config.team_display_name}
                )
            return httpx.Response(200, json={"value": []})
        payload = json.loads(request.content)
        assert payload["mentions"][0]["mentioned"]["application"]["id"] == config.bot_app_id
        if failure == "transport":
            raise httpx.ConnectError("lost")
        return httpx.Response(failure)

    class Tokens:
        def access_oid(self, persona: str, scopes: list[str]) -> str:
            return {"admin": "admin-oid", "member": "member-oid"}[persona]

        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("synthetic")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        clock = 0.0

        def now() -> float:
            return clock

        async def sleep(seconds: float) -> None:
            nonlocal clock
            clock += seconds

        graph = GraphTeamsDriver(config, cast(TokenStore, Tokens()), client, now=now, sleep=sleep)
        result = await send(graph, "admin", "channel", "hello")
    assert result["error"] == "post_outcome_unknown"
    assert len([call for call in calls if call.method == "POST"]) == 1
