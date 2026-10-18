"""Offline checks for the first Teams driver review fixes."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from qa.live.teams import __main__ as teams_cli
from qa.live.teams.access_flow_msal_signin import run as run_access_flow
from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.graph import CHAT, READ, SEND, TARGET, GraphTeamsDriver
from qa.live.teams.login import (
    AccessFlowAuthorizer,
    FlowFailed,
    login_with_access_flow,
    receive_redirect,
)
from qa.live.teams.tokens import ReauthRequired, Secret, TokenStore


@pytest.fixture
def config(tmp_path: Path) -> TeamsQaConfig:
    data = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    data["token_dir"] = str(tmp_path / "tokens")
    return TeamsQaConfig.model_validate(data)


def test_consented_scopes_only() -> None:
    approved = {
        "User.Read",
        "offline_access",
        "ChannelMessage.Send",
        "ChannelMessage.Read.All",
        "Chat.ReadWrite",
        "Team.ReadBasic.All",
        "Channel.ReadBasic.All",
    }
    assert CHAT == ["Chat.ReadWrite", "User.Read", "offline_access"]
    assert set(CHAT + READ + SEND + TARGET) <= approved


@pytest.mark.asyncio
async def test_flow_prints_one_fixed_line_when_cleanup_also_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Page:
        async def goto(self, url: str) -> None:
            raise RuntimeError("https://example.test/?code=FAKE_CODE")

    class Context:
        args = {"url": "https://example.test/?state=FAKE_STATE"}

    with pytest.raises(RuntimeError, match="msal_signin failed at: cleanup") as error:
        await run_access_flow(Page(), Context())
    assert capsys.readouterr().err.splitlines() == ["msal_signin failed at: cleanup"]
    assert "FAKE_CODE" not in str(error.value)


@pytest.mark.asyncio
async def test_loopback_ignores_preconnect_and_favicon() -> None:
    class Authorizer:
        async def authorize(self, auth_url: str, persona: str) -> None:
            port = int(auth_url)
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /favicon.ico HTTP/1.1\r\n\r\n")
            await writer.drain()
            assert b"404 Not Found" in await reader.read()
            writer.close()
            await writer.wait_closed()
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /?state=FAKE_STATE&code=FAKE_CODE HTTP/1.1\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    result = await receive_redirect(
        "FAKE_STATE", 2, Authorizer(), "", "admin", on_listen=lambda port: ("FAKE_STATE", str(port))
    )
    assert result["code"] == "FAKE_CODE"


@pytest.mark.asyncio
async def test_loopback_error_value_is_sanitized() -> None:
    class Authorizer:
        async def authorize(self, auth_url: str, persona: str) -> None:
            _reader, writer = await asyncio.open_connection("127.0.0.1", int(auth_url))
            writer.write(b"GET /?state=FAKE_STATE&error=invalid_grant HTTP/1.1\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    with pytest.raises(ReauthRequired, match="authorization error invalid_grant"):
        await receive_redirect(
            "FAKE_STATE",
            2,
            Authorizer(),
            "",
            "admin",
            on_listen=lambda port: ("FAKE_STATE", str(port)),
        )


@pytest.mark.asyncio
async def test_chat_candidate_sender_and_datetime_skew(config: TeamsQaConfig) -> None:
    since = datetime.now(UTC)
    reads: list[str] = []

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1.0/me/chats":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "one", "chatType": "oneOnOne", "members": []},
                        {"id": "two", "chatType": "oneOnOne", "members": []},
                    ]
                },
            )
        reads.append(path)
        if path.endswith("/one/messages"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "near",
                            "from": {"application": {"id": config.bot_app_id}},
                            "createdDateTime": (since - timedelta(seconds=5)).isoformat(),
                        },
                        {
                            "id": "old",
                            "createdDateTime": (since - timedelta(seconds=11)).isoformat(),
                        },
                    ]
                },
            )
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        messages = await driver.chat_messages_with_bot("admin", since)
    assert [message["id"] for message in messages] == ["near"]
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_find_correlation_allows_clock_skew(config: TeamsQaConfig) -> None:
    since = datetime.now(UTC)

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "near",
                        "body": {"content": "hello [qa:synthetic]"},
                        "createdDateTime": (since - timedelta(seconds=5)).isoformat(),
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        found = await driver.find_by_correlation("admin", "synthetic", since)
    assert found is not None and found["id"] == "near"


@pytest.mark.asyncio
async def test_reconciliation_waits_three_seconds(config: TeamsQaConfig) -> None:
    sleeps: list[float] = []
    posts = 0

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(503 if posts == 1 else 201, json={"id": "posted"})
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http, sleep=sleep)  # type: ignore[arg-type]
        await driver.post_mention("admin", "hello", "synthetic")
    assert posts == 2 and sleeps == [3]


@pytest.mark.asyncio
async def test_post_429_honors_retry_after(config: TeamsQaConfig) -> None:
    sleeps: list[float] = []
    posts = 0

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return (
            httpx.Response(429, headers={"Retry-After": "2"})
            if posts == 1
            else httpx.Response(201, json={"id": "posted"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http, sleep=sleep)  # type: ignore[arg-type]
        result = await driver.post_mention("admin", "hello", "synthetic")
    assert result["id"] == "posted" and posts == 2 and sleeps == [2]


@pytest.mark.asyncio
async def test_safe_flow_reason_survives_login(
    config: TeamsQaConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", (
                b"https://example.test/?code=FAKE_CODE\n"
                b"msal_signin failed at: fakecode\n"
                b"msal_signin failed at: password\n"
            )

    async def launch(*args: object, **kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    authorizer = AccessFlowAuthorizer("teams-qa", "synthetic", "admin@example.onmicrosoft.com")
    with pytest.raises(FlowFailed, match="msal_signin failed at: password") as error:
        await authorizer.authorize("https://example.test/?state=FAKE_STATE", "admin")
    assert "FAKE_CODE" not in str(error.value) and "FAKE_STATE" not in str(error.value)

    store = TokenStore(config)
    monkeypatch.setattr(store, "_client", lambda _persona: (object(), object()))

    async def fail_redirect(*args: object, **kwargs: object) -> None:
        raise FlowFailed("browser authorization failed (exit 1): msal_signin failed at: password")

    monkeypatch.setattr("qa.live.teams.login.receive_redirect", fail_redirect)
    with pytest.raises(ReauthRequired, match="msal_signin failed at: password"):
        await login_with_access_flow(store, "admin", ["User.Read"], authorizer)


def test_cli_error_classes_and_reauth_message(
    config: TeamsQaConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(TeamsQaConfig, "load", lambda _path: config)

    async def reauth(_args: object, _config: object) -> None:
        raise ReauthRequired("admin", "silent acquisition failed")

    monkeypatch.setattr(teams_cli, "execute", reauth)
    with pytest.raises(SystemExit):
        teams_cli.main(["status", "--persona", "admin"])
    assert "admin: reauthentication required (silent acquisition failed)" in capsys.readouterr().err

    async def unsafe(_args: object, _config: object) -> None:
        raise FlowFailed("FAKE_CODE")

    monkeypatch.setattr(teams_cli, "execute", unsafe)
    with pytest.raises(SystemExit):
        teams_cli.main(["status", "--persona", "admin"])
    output = capsys.readouterr().err
    assert output.strip() == "Teams QA command failed: FlowFailed"
