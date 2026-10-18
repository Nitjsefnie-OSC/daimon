"""Offline contract checks for bot-chat command journeys and smoke --all."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from qa.live.teams import __main__ as teams_cli
from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.evidence import Result
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

    def status(self, persona: str) -> dict[str, str]:
        return {"oid": "synthetic-user"}


@pytest.mark.asyncio
async def test_existing_chat_is_found_by_sender_app_id(config: TeamsQaConfig) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/me/chats":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "wrong-chat",
                            "chatType": "oneOnOne",
                            "members": [{"displayName": config.bot_display_name}],
                        },
                        {"id": "bot-chat", "chatType": "oneOnOne", "members": []},
                    ]
                },
            )
        if request.url.path.endswith("/bot-chat/messages"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "old-bot",
                            "from": {
                                "application": {
                                    "id": config.bot_app_id,
                                    "displayName": "Different display name",
                                }
                            },
                            "createdDateTime": datetime.now(UTC).isoformat(),
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        chat_id, snapshot = await driver.bot_chat("admin")
    assert chat_id == "bot-chat" and {m["id"] for m in snapshot} == {"old-bot"}


@pytest.mark.asyncio
async def test_no_existing_bot_chat_fails_without_post(config: TeamsQaConfig) -> None:
    requests: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.url.path == "/v1.0/me/chats":
            return httpx.Response(200, json={"value": []})
        raise AssertionError("unexpected Graph path")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        with pytest.raises(NoBotChat):
            await driver.bot_chat("admin")
    assert requests == ["GET"]


@pytest.mark.asyncio
async def test_chat_command_posts_exact_text_once_and_reconciles(config: TeamsQaConfig) -> None:
    clock = 0.0
    posts: list[dict[str, Any]] = []
    old = {
        "id": "old",
        "createdDateTime": datetime.now(UTC).isoformat(),
        "from": {"user": {"id": "synthetic-user"}},
        "body": {"content": "routines"},
    }
    new = {**old, "id": "new", "body": {"content": "<p>routines</p>"}}

    async def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(503)
        return httpx.Response(200, json={"value": [old, new] if clock >= 6 else [old]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http, now=lambda: clock, sleep=sleep)  # type: ignore[arg-type]
        result = await driver.post_chat_command("admin", "bot-chat", "routines", [old])
    assert result["id"] == "new" and clock == 6
    assert posts == [{"body": {"contentType": "text", "content": "routines"}}]


@pytest.mark.asyncio
async def test_unconfirmed_chat_post_is_never_repeated(config: TeamsQaConfig) -> None:
    clock = 0.0
    posts = 0

    async def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(503)
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http, now=lambda: clock, sleep=sleep)  # type: ignore[arg-type]
        with pytest.raises(PostOutcomeUnknown):
            await driver.post_chat_command("admin", "bot-chat", "routines", [])
    assert posts == 1 and clock == 30


@pytest.mark.asyncio
async def test_billing_admin_card_requires_add_credit(config: TeamsQaConfig) -> None:
    class Driver:
        def __init__(self) -> None:
            self.config = config
            self.posted = False

        def now(self) -> float:
            return 0.0

        async def sleep(self, seconds: float) -> None:
            return None

        def require_distinct_personas(self) -> dict[str, str]:
            return {"admin": "synthetic-admin", "member": "synthetic-member"}

        async def bot_chat(self, persona: str) -> tuple[str, list[dict[str, Any]]]:
            return "bot-chat", []

        async def post_chat_command(
            self, persona: str, chat_id: str, command: str, snapshot: list[dict[str, Any]]
        ) -> dict[str, str]:
            assert persona == "admin" and command == "billing" and chat_id == "bot-chat"
            self.posted = True
            return {"id": "posted", "createdDateTime": datetime.now(UTC).isoformat()}

        async def chat_messages(
            self, persona: str, chat_id: str, deadline: float | None = None
        ) -> list[dict[str, Any]]:
            return [
                {
                    "id": "card",
                    "createdDateTime": datetime.now(UTC).isoformat(),
                    "from": {"application": {"id": config.bot_app_id}},
                    "attachments": [
                        {
                            "content": {
                                "body": [
                                    {"type": "TextBlock", "text": "Billing"},
                                    {"type": "Action.Submit", "title": "Add credit"},
                                ]
                            }
                        }
                    ],
                }
            ]

    result = await run_journey(Driver(), resolve_journey(config, "billing-admin"))  # type: ignore[arg-type]
    assert result.classification == "allowed" and result.reply_message_ids == ["card"]


def test_smoke_all_writes_one_private_summary_serially(
    config: TeamsQaConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(TeamsQaConfig, "load", lambda _path: config)

    class Driver:
        def __init__(self, *_args: object) -> None:
            pass

        async def verify_target(self, persona: str) -> None:
            events.append(f"verify:{persona}")

    async def fake_run(_driver: object, journey: object) -> Result:
        persona = journey.persona  # type: ignore[attr-defined]
        command = journey.spec.command  # type: ignore[attr-defined]
        events.append(f"post:{persona}:{command}")
        return Result(
            "live",
            persona,
            "synthetic",
            "sent",
            "start",
            "end",
            ["card"],
            "",
            "allowed" if persona == "admin" else "refused",
        )

    monkeypatch.setattr(teams_cli, "GraphTeamsDriver", Driver)
    monkeypatch.setattr(teams_cli, "run_journey", fake_run)
    results_dir = tmp_path / "results"
    assert teams_cli.main(["smoke", "--all", "--results", str(results_dir), "--go"]) == 0
    assert events == [
        "verify:admin",
        "verify:member",
        "post:admin:routines",
        "post:member:routines",
        "post:admin:billing",
        "post:member:billing",
    ]
    files = list(results_dir.iterdir())
    assert len(files) == 1 and files[0].name.startswith("summary-")
    summary = json.loads(files[0].read_text())
    assert list(summary) == list(teams_cli.ALL_JOURNEYS)
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_smoke_all_requires_go_and_excludes_journey() -> None:
    with pytest.raises(SystemExit):
        teams_cli.main(["smoke", "--all", "--results", "/tmp/synthetic-results"])
    with pytest.raises(SystemExit):
        teams_cli.main(
            [
                "smoke",
                "--all",
                "--journey",
                "routines-admin",
                "--results",
                "/tmp/synthetic-results",
                "--go",
            ]
        )
