"""Staging test click: refused unless allowed, and otherwise the real click path runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from daimon.adapters.discord import test_click
from daimon.adapters.discord.test_click import (
    ClickRequest,
    TestClicker,
    enablement_refusal,
    start_test_click_server,
)
from daimon.adapters.discord.tool_confirmation import (
    _ConfirmationView,  # pyright: ignore[reportPrivateUsage]  # the real card view under test
)
from daimon.core.config import Settings
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
from daimon.core.posted_controls.confirmation import build_confirmation_card

TOKEN = "operator-token-for-tests"
GUILD, CHANNEL, CARD, BOT, QA, OTHER_QA, HUMAN = 10, 20, 30, 40, 111, 222, 333


def _settings(
    *,
    enabled: bool = True,
    token_hash: str | None = hashlib.sha256(TOKEN.encode()).hexdigest(),
    qa: tuple[str, ...] = (str(QA), str(OTHER_QA)),
) -> Settings:
    discord_settings = SimpleNamespace(
        test_click=enabled, test_click_token_sha256=token_hash, qa_bot_user_ids=qa
    )
    return cast(Settings, SimpleNamespace(discord=discord_settings))


def _user(user_id: int, *, bot: bool = False) -> dict[str, Any]:
    return {
        "id": str(user_id),
        "username": f"u{user_id}",
        "discriminator": "0",
        "avatar": None,
        "bot": bot,
    }


def _message() -> dict[str, Any]:
    return {
        "id": str(CARD),
        "channel_id": str(CHANNEL),
        "type": 0,
        "content": "",
        "author": _user(BOT, bot=True),
        "attachments": [],
        "embeds": [],
        "mentions": [],
        "mention_roles": [],
        "pinned": False,
        "mention_everyone": False,
        "tts": False,
        "timestamp": datetime.now(UTC).isoformat(),
        "edited_timestamp": None,
        "flags": 1 << 15,
        "components": [],
    }


def _channel_payload() -> dict[str, Any]:
    return {
        "id": str(CHANNEL),
        "type": 0,
        "guild_id": str(GUILD),
        "name": "v2-eval",
        "position": 0,
        "permission_overwrites": [],
        "nsfw": False,
        "parent_id": None,
    }


def _member(user_id: int) -> dict[str, Any]:
    return {
        "user": _user(user_id, bot=True),
        "roles": [],
        "joined_at": datetime.now(UTC).isoformat(),
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


def _not_found() -> discord.NotFound:
    return discord.NotFound(cast(Any, SimpleNamespace(status=404, reason="Not Found")), "Unknown")


class _FakeHTTP:
    """discord.py's HTTP client, minus Discord: records card edits and posts."""

    proxy = None
    proxy_auth = None

    def __init__(self) -> None:
        setattr(self, "_HTTPClient__session", None)  # noqa: B010  # Interaction reads the mangled name
        self.edits: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.members = {QA, OTHER_QA}

    async def get_message(self, channel_id: int, message_id: int) -> dict[str, Any]:
        assert (channel_id, message_id) == (CHANNEL, CARD)
        return _message()

    async def get_member(self, guild_id: int, user_id: int) -> dict[str, Any]:
        if user_id not in self.members:
            raise _not_found()
        return _member(user_id)

    async def get_channel(self, channel_id: int) -> dict[str, Any]:
        return _channel_payload()

    async def edit_message(self, channel_id: int, message_id: int, *, params: Any) -> Any:
        self.edits.append(params.payload)
        return _message()

    async def send_message(self, channel_id: int, *, params: Any) -> Any:
        self.posts.append(params.payload)
        return _message()


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield None

    async def append(self, _session: object, **fields: Any) -> None:
        self.events.append(fields)


class _World:
    """A real discord.py ConnectionState with one guild, channel and live card."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.client = discord.Client(intents=discord.Intents.none())
        # A started client has its loop; gateway events schedule listeners on it.
        self.client.loop = asyncio.get_running_loop()
        state = self.client._connection  # pyright: ignore[reportPrivateUsage]
        self.http = _FakeHTTP()
        state.http = self.http  # pyright: ignore[reportAttributeAccessIssue]
        state.user = discord.ClientUser(state=state, data=_user(BOT, bot=True))  # pyright: ignore[reportArgumentType]
        state.application_id = BOT
        guild = discord.Guild(
            data={  # pyright: ignore[reportArgumentType]
                "id": str(GUILD),
                "name": "staging",
                "roles": [
                    {
                        "id": str(GUILD),
                        "name": "@everyone",
                        "permissions": str(discord.Permissions.text().value),
                        "position": 0,
                        "color": 0,
                        "hoist": False,
                        "managed": False,
                        "mentionable": False,
                        "flags": 0,
                    }
                ],
                "preferred_locale": "en-US",
            },
            state=state,
        )
        self.channel = discord.TextChannel(state=state, guild=guild, data=_channel_payload())  # pyright: ignore[reportArgumentType]
        guild._add_channel(self.channel)  # pyright: ignore[reportPrivateUsage]
        state._add_guild(guild)  # pyright: ignore[reportPrivateUsage]
        self.audit = _Audit()
        monkeypatch.setattr(test_click, "append_event", self.audit.append)
        self.bot = SimpleNamespace(
            user=state.user,
            application_id=BOT,
            _connection=state,
            is_ready=lambda: True,
            get_channel=self._get_channel,
            fetch_channel=AsyncMock(side_effect=_not_found()),
            runtime=SimpleNamespace(sessionmaker=self.audit),
        )
        self.state = state

    def _get_channel(self, channel_id: int) -> discord.TextChannel | None:
        return self.channel if channel_id == CHANNEL else None

    def post_card(
        self,
        *,
        requester: int = QA,
        expires_in: timedelta = timedelta(minutes=10),
        admin: bool = False,
        check_admin: Any = None,
    ) -> asyncio.Future[ConfirmationAnswer]:
        prompt = ConfirmationPrompt(
            title="Switch daimon to GPT?",
            requester_platform_user_id=str(requester),
            expires_at=datetime.now(UTC) + expires_in,
            admin=admin,
        )
        answer: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
        card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh")
        view = _ConfirmationView(card, prompt, answer, check_admin)
        self.state.store_view(view, CARD)
        return answer

    def clicker(self, settings: Settings | None = None) -> TestClicker:
        return TestClicker(cast(Any, self.bot), settings or _settings())


def _request(**overrides: Any) -> ClickRequest:
    fields: dict[str, Any] = {
        "token": TOKEN,
        "operator": "nc-eval",
        "channel_id": CHANNEL,
        "message_id": CARD,
        "button": "Approve",
        "as_user_id": str(QA),
        **overrides,
    }
    return ClickRequest(**fields)


# ---- enablement ----


@pytest.mark.parametrize(
    ("settings", "project", "reason"),
    [
        (_settings(enabled=False), "example-staging", "test click is off"),
        (_settings(token_hash=None), "example-staging", "no operator token hash"),
        (_settings(qa=()), "example-staging", "no QA bot"),
        (_settings(), None, "not a staging host"),
        (_settings(), "example", "not a staging host"),
        (_settings(), "example-staging-not", "not a staging host"),
    ],
)
def test_enablement_is_refused_unless_flag_hash_qa_and_staging(
    settings: Settings, project: str | None, reason: str
) -> None:
    refusal = enablement_refusal(settings, project)
    assert refusal is not None and reason in refusal


def test_enablement_allows_a_staging_project_with_flag_and_hash() -> None:
    assert enablement_refusal(_settings(), "example-staging") is None


def test_settings_default_off() -> None:
    from daimon.core.config import DiscordSettings

    settings = DiscordSettings(bot_token="x")  # pyright: ignore[reportArgumentType]
    assert settings.test_click is False
    assert settings.test_click_token_sha256 is None


async def test_no_server_when_flag_off_and_no_metadata_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lookup = AsyncMock(return_value="example-staging")
    monkeypatch.setattr(test_click, "gcp_project_id", lookup)
    path = str(tmp_path / "s" / "sock")
    assert (
        await start_test_click_server(cast(Any, None), _settings(enabled=False), path=path) is None
    )
    lookup.assert_not_awaited()
    assert not Path(path).exists()


async def test_no_server_outside_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(test_click, "gcp_project_id", AsyncMock(return_value="example-prod"))
    path = str(tmp_path / "s" / "sock")
    assert await start_test_click_server(cast(Any, None), _settings(), path=path) is None
    assert not Path(path).exists()


# ---- refusals before any click ----


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"token": "wrong"}, "operator token rejected"),
        ({"as_user_id": str(HUMAN)}, "not an allow-listed QA bot"),
        ({"as_user_id": str(BOT)}, "not an allow-listed QA bot"),
        ({"button": "Details"}, "not a Confirm card answer button"),
        ({"button": "Always allow"}, "not a Confirm card answer button"),
        ({"message_id": CARD + 1}, "no live Confirm card"),
        ({"channel_id": CHANNEL + 1}, "channel not found"),
    ],
)
async def test_refusals_click_nothing_and_are_audited(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], reason: str
) -> None:
    world = _World(monkeypatch)
    answer = world.post_card()
    result = await world.clicker(_settings(qa=(str(QA), str(OTHER_QA), str(BOT)))).handle(
        _request(**overrides), peer_uid=1000
    )
    assert result["outcome"] == "denied"
    assert reason in result["reason"]
    assert not answer.done()
    assert world.http.edits == []
    if overrides.get("channel_id") is None:
        (event,) = world.audit.events
        assert event["tool_name"] == "discord_test_click"
        assert event["outcome"] == "denied"
        assert event["token_kind"] == "operator"
        assert event["target_message_id"] == str(overrides.get("message_id", CARD))
        assert "operator=nc-eval" in event["reason"]
        assert TOKEN not in json.dumps(event, default=str)


async def test_another_requesters_card_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    answer = world.post_card(requester=QA)
    result = await world.clicker().handle(_request(as_user_id=str(OTHER_QA)), peer_uid=1000)
    assert result["outcome"] == "denied"
    assert "not the card's requester" in result["reason"]
    assert not answer.done()
    assert world.http.edits == []


async def test_a_humans_card_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    answer = world.post_card(requester=HUMAN)
    for clicker in (str(QA), str(HUMAN)):
        result = await world.clicker().handle(_request(as_user_id=clicker), peer_uid=1000)
        assert result["outcome"] == "denied"
    assert not answer.done()


# ---- the real click path ----


async def test_allowed_click_runs_the_real_handler_and_edits_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    seen: list[discord.Interaction] = []
    world.client.event(_on_interaction(seen))
    answer = world.post_card()
    result = await world.clicker().handle(_request(), peer_uid=1000)
    assert result["outcome"] == "allowed", result
    assert await asyncio.wait_for(answer, 1) == "approved"
    (edit,) = world.http.edits
    assert "Approved" in json.dumps(edit)
    assert result["replies"] == [{"kind": "card_updated"}]
    (event,) = world.audit.events
    assert event["outcome"] == "allowed"
    assert event["platform_user_id"] == str(QA)
    assert event["operation"] == "Approve"
    # The same gateway event a real click raises, with the requester as user.
    await asyncio.sleep(0)
    assert [str(i.user.id) for i in seen] == [str(QA)]
    assert seen[0].type is discord.InteractionType.component


async def test_deny_answers_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    answer = world.post_card()
    result = await world.clicker().handle(_request(button="Deny"), peer_uid=1000)
    assert result["outcome"] == "allowed"
    assert await asyncio.wait_for(answer, 1) == "denied"


async def test_a_second_click_finds_no_live_card(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    world.post_card()
    assert (await world.clicker().handle(_request(), peer_uid=1000))["outcome"] == "allowed"
    second = await world.clicker().handle(_request(button="Deny"), peer_uid=1000)
    assert second["outcome"] == "denied"
    assert len(world.http.edits) == 1


async def test_admin_recheck_runs_with_the_requester(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    check_admin = AsyncMock(return_value="This change needs a current admin.")
    answer = world.post_card(admin=True, check_admin=check_admin)
    result = await world.clicker().handle(_request(button="Confirm"), peer_uid=1000)
    check_admin.assert_awaited_once()
    assert check_admin.await_args is not None and check_admin.await_args.args[1] == str(QA)
    assert result["outcome"] == "denied"
    assert result["reason"] == "the card's handler refused"
    assert result["replies"] == [
        {"kind": "ephemeral", "content": "This change needs a current admin."}
    ]
    assert not answer.done()
    assert world.http.edits == []


async def test_expired_card_is_refused_by_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    answer = world.post_card(expires_in=timedelta(seconds=-1))
    result = await world.clicker().handle(_request(), peer_uid=1000)
    assert result["outcome"] == "denied"
    assert result["replies"][0]["kind"] == "ephemeral"
    assert not answer.done()
    assert world.http.edits == []


def _on_interaction(seen: list[discord.Interaction]) -> Any:
    async def on_interaction(interaction: discord.Interaction) -> None:
        seen.append(interaction)

    return on_interaction


# ---- socket + CLI ----


async def test_socket_is_private_and_rejects_bad_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = _World(monkeypatch)
    monkeypatch.setattr(test_click, "gcp_project_id", AsyncMock(return_value="example-staging"))
    path = str(tmp_path / "s" / "sock")
    server = await start_test_click_server(cast(Any, world.bot), _settings(), path=path)
    assert server is not None
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(Path(path).parent).st_mode) == 0o700
        bad = await test_click._send(path, {"operator": "nc-eval"})  # pyright: ignore[reportPrivateUsage]
        assert bad["outcome"] == "denied" and "bad request" in bad["reason"]
        world.post_card()
        good = await test_click._send(  # pyright: ignore[reportPrivateUsage]
            path,
            {
                "token": TOKEN,
                "operator": "nc-eval",
                "channel_id": str(CHANNEL),
                "message_id": str(CARD),
                "button": "Approve",
                "as_user_id": str(QA),
            },
        )
        assert good["outcome"] == "allowed"
        assert world.audit.events[-1]["reason"].endswith(f"peer_uid={os.getuid()}")
    finally:
        server.close()
        await server.wait_closed()


def test_cli_without_a_server_says_it_is_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(readline=lambda: TOKEN + "\n"))
    code = test_click.main(
        [
            "--channel-id",
            "1",
            "--message-id",
            "2",
            "--button",
            "Confirm",
            "--as-user-id",
            "3",
            "--operator",
            "nc-eval",
            "--socket",
            str(tmp_path / "none.sock"),
        ]
    )
    assert code == 2
    assert "not running" in capsys.readouterr().out


async def test_no_server_in_a_redirected_socket_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(test_click, "gcp_project_id", AsyncMock(return_value="example-staging"))
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    path = str(tmp_path / "link" / "sock")
    assert await start_test_click_server(cast(Any, None), _settings(), path=path) is None
    assert not (tmp_path / "real" / "sock").exists()
