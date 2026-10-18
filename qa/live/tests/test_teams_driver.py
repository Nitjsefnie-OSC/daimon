"""Offline contract checks for the Teams persona driver."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from qa.live.teams import __main__ as teams_cli
from qa.live.teams import login as login_module
from qa.live.teams.__main__ import main
from qa.live.teams.access_flow_msal_signin import run as run_access_flow
from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.graph import GraphTeamsDriver, PostOutcomeUnknown
from qa.live.teams.journeys import card_details, resolve_journey, run_journey
from qa.live.teams.login import AccessFlowAuthorizer, receive_redirect
from qa.live.teams.tokens import ReauthRequired, Secret, TokenStore, access_scopes


@pytest.fixture
def config(tmp_path: Path) -> TeamsQaConfig:
    source = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    source["token_dir"] = str(tmp_path / "tokens")
    config = TeamsQaConfig.model_validate(source)
    config.token_dir.mkdir(mode=0o700)
    return config


class FakeCache:
    has_state_changed = True

    def serialize(self) -> str:
        return '{"AccessToken":{},"RefreshToken":{}}'


class FakeClient:
    def __init__(self, upn: str, tid: str, result: dict[str, Any]) -> None:
        self.upn, self.tid, self.result = upn, tid, result
        self.calls = 0

    def get_accounts(self) -> list[dict[str, str]]:
        return [{"username": self.upn, "realm": self.tid}]

    def acquire_token_silent(self, scopes: list[str], account: dict[str, str]) -> dict[str, Any]:
        self.calls += 1
        return self.result


def test_persona_isolation_and_identity(
    config: TeamsQaConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(config)
    admin = FakeClient(
        "admin@example.onmicrosoft.com",
        config.tenant_id,
        {
            "access_token": "FAKE_ACCESS",
            "id_token_claims": {
                "tid": config.tenant_id,
                "preferred_username": "admin@example.onmicrosoft.com",
            },
        },
    )
    member = FakeClient(
        "member@example.onmicrosoft.com",
        config.tenant_id,
        {
            "access_token": "FAKE_ACCESS",
            "id_token_claims": {
                "tid": config.tenant_id,
                "preferred_username": "admin@example.onmicrosoft.com",
            },
        },
    )
    monkeypatch.setattr(
        store, "_client", lambda persona: (admin if persona == "admin" else member, FakeCache())
    )
    assert str(store.acquire("admin", ["User.Read"])) == "<redacted>"
    with pytest.raises(ReauthRequired, match="account mismatch") as error:
        store.acquire("member", ["User.Read"])
    assert "FAKE_ACCESS" not in str(error.value)
    assert not store.path("member").exists()
    assert admin.calls == member.calls == 1


def test_secret_redaction() -> None:
    secret = Secret("FAKE_ACCESS")
    assert "FAKE_ACCESS" not in repr(secret) + str(secret) + f"{secret}"


def test_access_scopes_decodes_claims_locally() -> None:
    payload = base64.urlsafe_b64encode(b'{"scp":"Chat.Read User.Read"}').decode().rstrip("=")
    assert access_scopes(f"header.{payload}.signature") == {"Chat.Read", "User.Read"}
    assert access_scopes("opaque") == set()


def test_claim_mismatch_and_silent_failure(
    config: TeamsQaConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(config)
    for claim in (
        {"tid": "wrong", "preferred_username": "admin@example.onmicrosoft.com"},
        {"tid": config.tenant_id, "preferred_username": "other@example.onmicrosoft.com"},
    ):
        client = FakeClient(
            "admin@example.onmicrosoft.com",
            config.tenant_id,
            {"access_token": "FAKE_ACCESS", "id_token_claims": claim},
        )
        monkeypatch.setattr(store, "_client", lambda _persona, client=client: (client, FakeCache()))
        with pytest.raises(ReauthRequired):
            store.acquire("admin", ["User.Read"])
        assert not store.path("admin").exists()
    client.result = {"error": "invalid_grant", "error_description": "FAKE_REFRESH"}
    with pytest.raises(ReauthRequired) as error:
        store.acquire("admin", ["User.Read"])
    assert client.calls == 2
    assert "FAKE_REFRESH" not in str(error.value)


@pytest.mark.asyncio
async def test_post_reconciles_ambiguous_failure(config: TeamsQaConfig) -> None:
    posts = 0
    clock = 0.0
    found = False
    show_after_post = False

    async def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posts, found
        if request.method == "POST":
            posts += 1
            if show_after_post:
                found = True
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "found",
                        "body": {"content": "[qa:synthetic]"},
                        "createdDateTime": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
                    }
                ]
                if found
                else []
            },
        )

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http, now=lambda: clock, sleep=sleep)  # type: ignore[arg-type]
        with pytest.raises(PostOutcomeUnknown):
            await driver.post_mention("admin", "hello", "synthetic")
        assert posts == 1 and clock == 30
        show_after_post = True
        posts = 0
        result = await driver.post_mention("admin", "hello", "synthetic")
        assert result["id"] == "found" and posts == 1


@pytest.mark.asyncio
async def test_verify_target_refuses_mismatch(config: TeamsQaConfig) -> None:
    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "wrong", "displayName": "wrong"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="target mismatch"):
            await driver.verify_target("admin")


@pytest.mark.asyncio
async def test_loopback_state_mismatch() -> None:
    class Authorizer:
        async def authorize(self, auth_url: str, persona: str) -> None:
            _reader, writer = await asyncio.open_connection("127.0.0.1", int(auth_url))
            writer.write(b"GET /?state=FAKE_BAD_STATE&code=FAKE_CODE HTTP/1.1\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    with pytest.raises(ReauthRequired, match="state mismatch") as error:
        await receive_redirect(
            "FAKE_GOOD_STATE",
            1,
            Authorizer(),
            "",
            "admin",
            on_listen=lambda port: ("FAKE_GOOD_STATE", str(port)),
        )
    assert "FAKE_CODE" not in str(error.value)


@pytest.mark.asyncio
async def test_loopback_timeout() -> None:
    class Authorizer:
        async def authorize(self, auth_url: str, persona: str) -> None:
            await asyncio.sleep(1)

    with pytest.raises(ReauthRequired, match="timed out"):
        await receive_redirect("FAKE_STATE", 0.01, Authorizer(), "", "admin")


@pytest.mark.asyncio
async def test_access_flow_output_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Process:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"https://example.test/?code=FAKE_CODE", b"FAKE_REFRESH"

    async def launch(*args: object, **kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(RuntimeError) as error:
        await AccessFlowAuthorizer(
            "teams-qa", "synthetic", "admin@example.onmicrosoft.com"
        ).authorize("https://example.test/?state=FAKE_STATE", "admin")
    printed = capsys.readouterr()
    combined = str(error.value) + printed.out + printed.err
    assert all(secret not in combined for secret in ("FAKE_CODE", "FAKE_REFRESH", "FAKE_STATE"))


@pytest.mark.asyncio
async def test_saved_flow_clears_tab_and_bounds_totp_handoff() -> None:
    class Locator:
        def __init__(self, page: Page, name: str) -> None:
            self.page, self.name = page, name

        async def is_visible(self) -> bool:
            return self.name == self.page.state

        async def get_attribute(self, name: str) -> str | None:
            return "Enter code" if name == "aria-label" else None

    class Page:
        visited: list[str] = []
        state = "input[name=otc]"

        async def goto(self, url: str) -> None:
            self.visited.append(url)

        def locator(self, selector: str) -> Locator:
            return Locator(self, selector)

        def get_by_role(self, role: str, name: str) -> Locator:
            return Locator(self, name)

        def get_by_text(self, text: str) -> Locator:
            return Locator(self, text)

    class Context:
        args = {
            "url": "https://example.test/?state=FAKE_STATE",
            "upn": "admin@example.onmicrosoft.com",
            "item": "synthetic",
            "persona": "admin",
        }
        last_card_id = "synthetic-card"
        handoffs: list[object] = []

        async def fill(self, item: str, field: str, label: str, submit: bool) -> None:
            raise RuntimeError("FAKE_CODE")

        async def handoff(self, *args: object, **kwargs: object) -> None:
            self.handoffs.append((args, kwargs))
            if len(self.handoffs) == 1:
                raise RuntimeError("FAKE_REFRESH")
            page.state = "sign-in complete, you can close this tab"

        async def expect(self, locator: Locator) -> None:
            assert await locator.is_visible()

    page, ctx = Page(), Context()
    await run_access_flow(page, ctx)
    assert page.visited[-1] == "about:blank"
    assert len(ctx.handoffs) == 2
    assert ctx.handoffs[1][1]["retry_of"] == "synthetic-card"


@pytest.mark.asyncio
async def test_saved_flow_sanitizes_failure_and_clears_tab() -> None:
    class Page:
        visited: list[str] = []

        async def goto(self, url: str) -> None:
            self.visited.append(url)
            if url != "about:blank":
                raise RuntimeError("https://example.test/?code=FAKE_CODE")

    class Context:
        args = {"url": "https://example.test/?state=FAKE_STATE"}

    page = Page()
    with pytest.raises(RuntimeError, match="msal_signin failed at: open") as error:
        await run_access_flow(page, Context())
    assert page.visited[-1] == "about:blank"
    assert "FAKE_CODE" not in str(error.value) and "FAKE_STATE" not in str(error.value)


@pytest.mark.asyncio
async def test_http_400_never_reposts(config: TeamsQaConfig) -> None:
    calls = 0

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            calls += 1
            return httpx.Response(400, json={"error": "FAKE_CODE"})
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="HTTP 400") as error:
            await driver.post_mention("admin", "hi", "synthetic")
    assert calls == 1 and "FAKE_CODE" not in str(error.value)


@pytest.mark.asyncio
async def test_command_post_has_exact_text_and_reconciles_by_author(
    config: TeamsQaConfig,
) -> None:
    config.personas["admin"].expected_oid = "00000000-0000-0000-0000-000000000005"
    posts: list[str] = []

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        if request.method == "POST":
            posts.append(json.loads(request.content)["body"]["content"])
            posted = True
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "found",
                        "body": {"content": '<at id="0">Example Bot</at> routines'},
                        "from": {"user": {"id": config.personas["admin"].expected_oid}},
                        "createdDateTime": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
                    }
                ]
                if posted
                else []
            },
        )

    posted = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http)  # type: ignore[arg-type]
        result = await driver.post_mention("admin", "routines", "synthetic", command=True)
    assert result["id"] == "found"
    assert posts == ['<at id="0">Example Bot</at> routines']


@pytest.mark.asyncio
async def test_routines_card_role_assertions(config: TeamsQaConfig) -> None:
    class Driver:
        posted = False

        def now(self) -> float:
            return 0.0 if not self.posted else 1.0

        async def sleep(self, seconds: float) -> None:
            return None

        async def channel_message_ids(self, persona: str) -> frozenset[str]:
            return frozenset()

        async def post_mention(
            self,
            persona: str,
            text: str,
            correlation: str,
            *,
            command: bool = False,
            seen_ids: frozenset[str] | None = None,
        ) -> dict[str, str]:
            assert command and text == "routines"
            self.posted = True
            return {"id": "posted"}

        async def read_replies(
            self, persona: str, message_id: str, deadline: float
        ) -> list[dict[str, object]]:
            return [
                {
                    "id": "pointer",
                    "createdDateTime": datetime.now(UTC).isoformat(),
                    "body": {"content": "I've answered `routines` in our 1:1 chat."},
                }
            ]

        async def chat_messages_with_bot(
            self, persona: str, since: datetime, deadline: float | None = None
        ) -> list[dict[str, object]]:
            if not self.posted:
                return []
            actions = [{"type": "Action.Submit", "title": "Refresh"}]
            if persona == "admin":
                actions.append({"type": "Action.Submit", "title": "New routine"})
            content = {
                "body": [
                    {"type": "TextBlock", "text": "Routines"},
                    {"type": "ActionSet", "actions": actions},
                ]
            }
            return [
                {
                    "id": "card",
                    "createdDateTime": datetime.now(UTC).isoformat(),
                    "body": {"content": ""},
                    "attachments": [{"content": json.dumps(content)}],
                }
            ]

    admin = await run_journey(Driver(), resolve_journey(config, "routines-admin"))  # type: ignore[arg-type]
    member = await run_journey(Driver(), resolve_journey(config, "routines-member"))  # type: ignore[arg-type]
    assert admin.classification == "allowed" and "New routine" in admin.action_titles
    assert member.classification == "refused" and "New routine" not in member.action_titles
    assert admin.reply_message_ids == ["pointer", "card"]
    assert card_details({"attachments": [{"content": '{"body":[]}'}]}) == (set(), set())


@pytest.mark.asyncio
async def test_reply_poll_deadline(config: TeamsQaConfig) -> None:
    clock = 0.0
    calls = 0

    class Tokens:
        def acquire(self, persona: str, scopes: list[str]) -> Secret:
            return Secret("FAKE_ACCESS")

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"value": []})

    async def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        driver = GraphTeamsDriver(config, Tokens(), http, now=lambda: clock, sleep=sleep)  # type: ignore[arg-type]
        assert await driver.wait_for_reply("admin", "synthetic", lambda _r: True, 3) is None
    assert clock == 3 and calls <= 3


def test_smoke_guards(capsys: pytest.CaptureFixture[str]) -> None:
    for args in (["smoke"], ["smoke", "--go", "--max-turns", "5"]):
        with pytest.raises(SystemExit):
            main(args)
    output = capsys.readouterr()
    assert "FAKE_ACCESS" not in output.out + output.err


@pytest.mark.parametrize(
    "command, extra",
    [
        ("install-flow", []),
        ("login", ["--persona", "admin"]),
        ("status", ["--persona", "admin"]),
        ("logout", ["--persona", "admin"]),
        ("verify-target", ["--persona", "admin"]),
        ("smoke", ["--journey", "synthetic", "--results", "/tmp/teams-qa-test", "--go"]),
    ],
)
def test_cli_redacts_failures(
    command: str,
    extra: list[str],
    config: TeamsQaConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(TeamsQaConfig, "load", lambda _path: config)

    async def fail(_args: object, _config: object) -> None:
        raise RuntimeError("FAKE_ACCESS FAKE_REFRESH FAKE_CODE FAKE_STATE")

    monkeypatch.setattr(teams_cli, "execute", fail)
    with pytest.raises(SystemExit):
        main([command, *extra])
    output = capsys.readouterr()
    assert all(
        value not in output.out + output.err
        for value in ("FAKE_ACCESS", "FAKE_REFRESH", "FAKE_CODE", "FAKE_STATE")
    )


class FakeManualClient:
    """MSAL auth-code calls without discovery or token endpoint requests."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.responses: list[dict[str, str]] = []

    def initiate_auth_code_flow(self, scopes: list[str], **kwargs: object) -> dict[str, str]:
        assert kwargs == {
            "redirect_uri": "http://localhost",
            "login_hint": "admin@example.onmicrosoft.com",
            "prompt": "login",
        }
        return {"auth_uri": "https://example.test/authorize", "state": "EXPECTED_STATE"}

    def acquire_token_by_auth_code_flow(
        self, flow: dict[str, str], response: dict[str, str]
    ) -> dict[str, Any]:
        self.responses.append(response)
        return {
            "access_token": "FAKE_ACCESS",
            "id_token_claims": {
                "tid": self.tenant_id,
                "preferred_username": "admin@example.onmicrosoft.com",
            },
        }


@pytest.fixture
def manual_login_setup(
    config: TeamsQaConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TokenStore, FakeManualClient]:
    store = TokenStore(config)
    client = FakeManualClient(config.tenant_id)
    monkeypatch.setattr(store, "_client", lambda _persona: (client, FakeCache()))
    monkeypatch.setattr(login_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    return store, client


def test_manual_login_valid_redirect_is_private(
    manual_login_setup: tuple[TokenStore, FakeManualClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, client = manual_login_setup
    monkeypatch.setattr(
        login_module.getpass,
        "getpass",
        lambda _prompt: "http://localhost/?state=EXPECTED_STATE&code=PASTED_CODE",
    )
    login_module.manual_login(store, "admin", ["User.Read"])
    output = capsys.readouterr()
    assert client.responses == [{"state": "EXPECTED_STATE", "code": "PASTED_CODE"}]
    assert "https://example.test/authorize" in output.out
    assert "private window" in output.out
    assert all(
        value not in output.out + output.err
        for value in ("PASTED_CODE", "EXPECTED_STATE", "FAKE_ACCESS")
    )


@pytest.mark.parametrize(
    "address, reason, pasted_state",
    [
        ("http://localhost/?state=PASTED_STATE&code=PASTED_CODE", "state mismatch", "PASTED_STATE"),
        (
            "http://localhost/?state=EXPECTED_STATE&error=invalid_grant&code=PASTED_CODE",
            "authorization error invalid_grant",
            "EXPECTED_STATE",
        ),
        (
            "https://localhost/?state=EXPECTED_STATE&code=PASTED_CODE",
            "redirect address mismatch",
            "EXPECTED_STATE",
        ),
        (
            "http://wrong.example.test/?state=EXPECTED_STATE&code=PASTED_CODE",
            "redirect address mismatch",
            "EXPECTED_STATE",
        ),
        ("http://localhost/?state=EXPECTED_STATE", "authorization code missing", "EXPECTED_STATE"),
    ],
)
def test_manual_login_rejects_paste_without_disclosure(
    manual_login_setup: tuple[TokenStore, FakeManualClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    address: str,
    reason: str,
    pasted_state: str,
) -> None:
    store, client = manual_login_setup
    monkeypatch.setattr(login_module.getpass, "getpass", lambda _prompt: address)
    with pytest.raises(ReauthRequired, match=reason) as error:
        login_module.manual_login(store, "admin", ["User.Read"])
    output = capsys.readouterr()
    visible = output.out + output.err + str(error.value)
    assert "PASTED_CODE" not in visible and pasted_state not in visible
    assert client.responses == []


def test_manual_login_requires_tty_before_prompt_or_msal(
    manual_login_setup: tuple[TokenStore, FakeManualClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, client = manual_login_setup
    monkeypatch.setattr(login_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))

    def no_prompt(_prompt: str) -> str:
        raise AssertionError("getpass must not run without a TTY")

    monkeypatch.setattr(login_module.getpass, "getpass", no_prompt)
    with pytest.raises(ReauthRequired, match="manual login requires a TTY") as error:
        login_module.manual_login(store, "admin", ["User.Read"])
    output = capsys.readouterr()
    assert client.responses == []
    assert all(
        value not in output.out + output.err + str(error.value)
        for value in ("PASTED_CODE", "PASTED_STATE")
    )


def _jwt(claims: dict[str, str]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_silent_refresh_checks_access_token_identity(
    config: TeamsQaConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached refresh has no ID-token claims; the access token's own claims decide."""
    store = TokenStore(config)
    upn = "admin@example.onmicrosoft.com"
    good = _jwt({"tid": config.tenant_id, "upn": upn})
    client = FakeClient(upn, config.tenant_id, {"access_token": good})
    monkeypatch.setattr(store, "_client", lambda _persona: (client, FakeCache()))
    assert store.acquire("admin", ["User.Read"]).value == good
    for claims in (
        {"tid": config.tenant_id, "upn": "member@example.onmicrosoft.com"},
        {"tid": "wrong", "upn": upn},
        {},
    ):
        client.result = {"access_token": _jwt(claims) if claims else "opaque"}
        with pytest.raises(ReauthRequired) as error:
            store.acquire("admin", ["User.Read"])
        assert "header." not in str(error.value)
