"""Offline regressions for one-shot posting and built-in journey matching."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.graph import GraphTeamsDriver, NoBotChat, PostOutcomeUnknown
from qa.live.teams.journeys import resolve_journey, run_journey
from qa.live.teams.tokens import Secret


@pytest.fixture
def config(tmp_path: Path) -> TeamsQaConfig:
    source = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    source["token_dir"] = str(tmp_path / "tokens")
    return TeamsQaConfig.model_validate(source)


class Tokens:
    def acquire(self, persona: str, scopes: list[str]) -> Secret:
        return Secret("FAKE_ACCESS")


@pytest.mark.asyncio
async def test_prior_exact_command_is_excluded_from_reconciliation(config: TeamsQaConfig) -> None:
    config.personas["admin"].expected_oid = "synthetic-author"
    clock = 0.0
    posts = 0
    prior = {
        "id": "prior",
        "createdDateTime": datetime.now(UTC).isoformat(),
        "from": {"user": {"id": "synthetic-author"}},
        "body": {"content": '<at id="0">Example Bot</at> routines'},
    }

    async def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(503)
        return httpx.Response(200, json={"value": [prior]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http, now=lambda: clock, sleep=sleep)  # type: ignore[arg-type]
        with pytest.raises(PostOutcomeUnknown):
            await driver.post_mention("admin", "routines", "synthetic", command=True)
    assert posts == 1 and clock == 30


@pytest.mark.asyncio
async def test_transport_failure_also_sends_only_once(config: TeamsQaConfig) -> None:
    clock = 0.0
    posts = 0

    async def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            raise httpx.ConnectError("FAKE_ACCESS", request=request)
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http, now=lambda: clock, sleep=sleep)  # type: ignore[arg-type]
        with pytest.raises(PostOutcomeUnknown) as error:
            await driver.post_mention("admin", "routines", "synthetic", command=True)
    assert posts == 1 and clock == 30 and "FAKE_ACCESS" not in str(error.value)


@pytest.mark.asyncio
async def test_bot_chat_uses_application_id_and_absence_is_empty(config: TeamsQaConfig) -> None:
    now = datetime.now(UTC).isoformat()
    messages: list[dict[str, Any]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/me/chats":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "one",
                            "chatType": "oneOnOne",
                            "members": [{"displayName": config.bot_display_name}],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"value": messages})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        assert await driver.chat_messages_with_bot("admin", datetime.now(UTC)) == []
        messages.append(
            {
                "id": "card",
                "createdDateTime": now,
                "from": {
                    "application": {"id": config.bot_app_id, "displayName": "Daimon  Staging "}
                },
            }
        )
        assert [
            item["id"] for item in await driver.chat_messages_with_bot("admin", datetime.now(UTC))
        ] == ["card"]


@pytest.mark.asyncio
async def test_two_bot_chats_remain_ambiguous(config: TeamsQaConfig) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/me/chats":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "one", "chatType": "oneOnOne"},
                        {"id": "two", "chatType": "oneOnOne"},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "card",
                        "createdDateTime": datetime.now(UTC).isoformat(),
                        "from": {"application": {"id": config.bot_app_id}},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="bot chat ambiguous"):
            await driver.chat_messages_with_bot("admin", datetime.now(UTC))


def test_only_builtin_journeys_are_accepted(config: TeamsQaConfig) -> None:
    with pytest.raises(ValueError, match="unknown built-in journey"):
        resolve_journey(config, "custom")
    data = config.model_dump(mode="json")
    data["journeys"] = {"custom": {"persona": "admin", "command": "hello"}}
    with pytest.raises(ValidationError):
        TeamsQaConfig.model_validate(data)


class JourneyDriver:
    def __init__(self, config: TeamsQaConfig, cards: list[dict[str, Any]] | None = None) -> None:
        self.config = config
        self.clock = 0.0
        self.posted = False
        self.cards = cards or []

    def now(self) -> float:
        return self.clock

    async def sleep(self, seconds: float) -> None:
        self.clock += seconds

    async def bot_chat(self, persona: str) -> tuple[str, list[dict[str, Any]]]:
        return "bot-chat", self.cards

    async def post_chat_command(
        self, persona: str, chat_id: str, text: str, seen_ids: frozenset[str]
    ) -> dict[str, str]:
        assert chat_id == "bot-chat" and text == "routines"
        self.posted = True
        return {"id": "posted"}

    async def read_replies(
        self, persona: str, message_id: str, deadline: float
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "pointer",
                "createdDateTime": datetime.now(UTC).isoformat(),
                "body": {"content": "I've answered `routines` in our 1:1 chat."},
            }
        ]

    async def chat_messages(
        self, persona: str, chat_id: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        return self.cards


@pytest.mark.asyncio
async def test_missing_chat_fails_before_post(config: TeamsQaConfig) -> None:
    class MissingDriver(JourneyDriver):
        async def bot_chat(self, persona: str) -> tuple[str, list[dict[str, Any]]]:
            raise NoBotChat("no bot chat")

    driver = MissingDriver(config)
    result = await run_journey(driver, resolve_journey(config, "routines-admin"), timeout=3)  # type: ignore[arg-type]
    assert result.classification == "error"
    assert result.error_reason == "no bot chat: sign in to Teams once as this persona"
    assert not driver.posted and driver.clock == 0


@pytest.mark.asyncio
async def test_prior_card_inside_skew_window_is_ignored(config: TeamsQaConfig) -> None:
    old_card = {
        "id": "old",
        "createdDateTime": (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
        "attachments": [
            {
                "content": {
                    "body": [
                        {"type": "TextBlock", "text": "Routines"},
                        {"type": "Action.Submit", "title": "New routine"},
                    ]
                }
            }
        ],
    }
    driver = JourneyDriver(config, [old_card])
    result = await run_journey(driver, resolve_journey(config, "routines-admin"), timeout=3)  # type: ignore[arg-type]
    assert result.classification == "timeout" and driver.clock == 3


@pytest.mark.asyncio
async def test_full_card_text_is_checked_and_error_reason_is_safe(config: TeamsQaConfig) -> None:
    full_text = "x" * 320 + " Routines"
    card = {
        "id": "new",
        "createdDateTime": datetime.now(UTC).isoformat(),
        "from": {"application": {"id": config.bot_app_id}},
        "attachments": [
            {
                "content": {
                    "body": [
                        {"type": "TextBlock", "text": full_text},
                        {"type": "Action.Submit", "title": "New routine"},
                    ]
                }
            }
        ],
    }

    class FreshDriver(JourneyDriver):
        async def chat_messages(
            self, persona: str, chat_id: str, deadline: float | None = None
        ) -> list[dict[str, Any]]:
            return [card] if self.posted else []

    result = await run_journey(
        FreshDriver(config),
        resolve_journey(config, "routines-admin"),
        timeout=3,  # type: ignore[arg-type]
    )
    assert result.classification == "allowed" and result.error_reason is None
    assert len(result.reply_excerpt) == 300

    class BrokenDriver(JourneyDriver):
        async def bot_chat(self, persona: str) -> tuple[str, list[dict[str, Any]]]:
            raise RuntimeError("FAKE_ACCESS https://example.test/?code=FAKE_CODE")

    failed = await run_journey(
        BrokenDriver(config),
        resolve_journey(config, "routines-admin"),
        timeout=3,  # type: ignore[arg-type]
    )
    assert failed.classification == "error"
    assert failed.error_reason == "RuntimeError: journey operation failed"
    assert "FAKE_ACCESS" not in failed.error_reason and "FAKE_CODE" not in failed.error_reason
