"""Offline regressions for quiet chat, server ordering, and persona identity."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.graph import GraphTeamsDriver
from qa.live.teams.journeys import chat_is_quiet, resolve_journey, run_journey
from qa.live.teams.tokens import Secret, TokenStore


@pytest.fixture
def config(tmp_path: Path) -> TeamsQaConfig:
    source = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    source["token_dir"] = str(tmp_path / "tokens")
    return TeamsQaConfig.model_validate(source)


def message(
    message_id: str,
    created: datetime,
    *,
    user: str | None = None,
    bot: str | None = None,
    text: str = "",
) -> dict[str, Any]:
    sender = {"user": {"id": user}} if user else {"application": {"id": bot}}
    return {
        "id": message_id,
        "createdDateTime": created.isoformat(),
        "from": sender,
        "body": {"content": text},
    }


def card(message_id: str, created: datetime, bot_id: str, *, refresh: bool) -> dict[str, Any]:
    actions = [{"type": "Action.Submit", "title": "Refresh"}] if refresh else []
    return {
        **message(message_id, created, bot=bot_id),
        "attachments": [
            {
                "content": {
                    "body": [
                        {"type": "TextBlock", "text": "Routines"},
                        {"type": "ActionSet", "actions": actions},
                    ]
                }
            }
        ],
    }


class JourneyDriver:
    def __init__(
        self,
        config: TeamsQaConfig,
        snapshot: list[dict[str, Any]],
        replies: list[dict[str, Any]],
        posted_at: datetime,
    ) -> None:
        self.config = config
        self.snapshot = snapshot
        self.replies = replies
        self.posted_at = posted_at
        self.posts = 0
        self.clock = 0.0

    def require_distinct_personas(self) -> dict[str, str]:
        return {"admin": "synthetic-admin", "member": "synthetic-member"}

    def now(self) -> float:
        return self.clock

    async def sleep(self, seconds: float) -> None:
        self.clock += seconds

    async def bot_chat(self, persona: str) -> tuple[str, list[dict[str, Any]]]:
        return "bot-chat", self.snapshot

    async def post_chat_command(
        self, persona: str, chat_id: str, command: str, snapshot: list[dict[str, Any]]
    ) -> dict[str, str]:
        assert snapshot is self.snapshot
        self.posts += 1
        return {"id": "sent", "createdDateTime": self.posted_at.isoformat()}

    async def chat_messages(
        self, persona: str, chat_id: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        return self.replies


@pytest.mark.asyncio
async def test_pending_recent_command_refuses_without_post(config: TeamsQaConfig) -> None:
    now = datetime.now(UTC)
    pending = message("pending", now - timedelta(seconds=5), user="synthetic-admin")
    driver = JourneyDriver(config, [pending], [], now)
    result = await run_journey(driver, resolve_journey(config, "routines-admin"), timeout=3)  # type: ignore[arg-type]
    assert result.classification == "error" and result.error_reason == "chat not quiet"
    assert driver.posts == 0

    answered = message("answer", now - timedelta(seconds=4), bot=config.bot_app_id)
    assert chat_is_quiet([pending, answered], "synthetic-admin", config.bot_app_id, now)
    assert chat_is_quiet(
        [message("old", now - timedelta(seconds=31), user="synthetic-admin")],
        "synthetic-admin",
        config.bot_app_id,
        now,
    )


@pytest.mark.asyncio
async def test_late_prior_card_before_server_post_time_is_ignored(config: TeamsQaConfig) -> None:
    now = datetime.now(UTC)
    prior = card("prior", now - timedelta(seconds=1), config.bot_app_id, refresh=True)
    driver = JourneyDriver(config, [], [prior], now)
    result = await run_journey(driver, resolve_journey(config, "routines-member"), timeout=3)  # type: ignore[arg-type]
    assert result.classification == "timeout" and driver.posts == 1 and driver.clock == 3


@pytest.mark.asyncio
async def test_member_outage_card_is_error_without_refresh(config: TeamsQaConfig) -> None:
    now = datetime.now(UTC)
    outage = card("outage", now + timedelta(seconds=1), config.bot_app_id, refresh=False)
    driver = JourneyDriver(config, [], [outage], now)
    result = await run_journey(driver, resolve_journey(config, "routines-member"), timeout=3)  # type: ignore[arg-type]
    assert result.classification == "error"
    assert result.error_reason == "CardValidationError: card did not match expected actions"
    assert driver.posts == 1

    valid = card("member", now + timedelta(seconds=1), config.bot_app_id, refresh=True)
    allowed_driver = JourneyDriver(config, [], [valid], now)
    allowed = await run_journey(
        allowed_driver,
        resolve_journey(config, "routines-member"),
        timeout=3,  # type: ignore[arg-type]
    )
    assert allowed.classification == "refused"

    misleading = card("misleading", now + timedelta(seconds=1), config.bot_app_id, refresh=True)
    misleading["attachments"][0]["content"]["body"][0]["text"] = "Routines temporarily unavailable"
    misleading_driver = JourneyDriver(config, [], [misleading], now)
    failed = await run_journey(
        misleading_driver,
        resolve_journey(config, "routines-member"),
        timeout=3,  # type: ignore[arg-type]
    )
    assert failed.classification == "error"


@pytest.mark.asyncio
async def test_reconciliation_rejects_unseen_message_older_than_snapshot(
    config: TeamsQaConfig,
) -> None:
    now = datetime.now(UTC)
    newest = message("snapshot", now + timedelta(seconds=3), user="synthetic-admin", text="other")
    prior = message("prior", now + timedelta(seconds=2), user="synthetic-admin", text="routines")
    clock = 0.0
    posts = 0

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

        def status(self, persona: str) -> dict[str, str]:
            return {"oid": "synthetic-admin"}

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
        with pytest.raises(RuntimeError, match="POST outcome unknown"):
            await driver.post_chat_command("admin", "bot-chat", "routines", [newest])
    assert posts == 1 and clock == 30


@pytest.mark.parametrize(
    "field, value",
    [
        ("upn", "ADMIN@example.onmicrosoft.com"),
        ("expected_oid", "SYNTHETIC-OID"),
    ],
)
def test_config_rejects_duplicate_personas(config: TeamsQaConfig, field: str, value: str) -> None:
    data = config.model_dump(mode="json")
    if field == "expected_oid":
        data["personas"]["admin"]["expected_oid"] = "synthetic-oid"
    data["personas"]["member"][field] = value
    with pytest.raises(ValidationError, match="must be distinct"):
        TeamsQaConfig.model_validate(data)


def test_access_oid_comes_from_access_token(
    config: TeamsQaConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = base64.urlsafe_b64encode(b'{"oid":"synthetic-admin"}').decode().rstrip("=")
    store = TokenStore(config)
    monkeypatch.setattr(store, "acquire", lambda _persona, _scopes: Secret(f"h.{claims}.s"))
    assert store.access_oid("admin", ["Chat.ReadWrite"]) == "synthetic-admin"


@pytest.mark.asyncio
async def test_matching_access_token_oids_refuse_before_graph(config: TeamsQaConfig) -> None:
    class Tokens:
        def access_oid(self, persona: str, scopes: list[str]) -> str:
            return "same-oid"

    def transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Graph must not be called for identical personas")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        result = await run_journey(driver, resolve_journey(config, "routines-admin"))
    assert result.classification == "error"
    assert result.error_reason == "PersonaCollision: admin and member token identities match"
