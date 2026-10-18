"""Manual paste login is offline, private, and the default CLI mode."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qa.live.teams import __main__ as teams_cli
from qa.live.teams import login as login_module
from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.tokens import ReauthRequired, TokenStore


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TokenStore, object]:
    source = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    source["token_dir"] = str(tmp_path / "tokens")
    config = TeamsQaConfig.model_validate(source)
    config.token_dir.mkdir(mode=0o700)
    store = TokenStore(config)

    class Cache:
        def serialize(self) -> str:
            return "{}"

    class Client:
        response: dict[str, str] | None = None
        calls = 0

        def initiate_auth_code_flow(self, scopes: list[str], **kwargs: object) -> dict[str, str]:
            assert kwargs == {
                "redirect_uri": "http://localhost",
                "login_hint": "admin@example.onmicrosoft.com",
                "prompt": "login",
            }
            return {
                "auth_uri": "https://example.test/authorize?state=FAKE_STATE",
                "state": "FAKE_STATE",
            }

        def acquire_token_by_auth_code_flow(
            self, flow: dict[str, str], response: dict[str, str]
        ) -> dict[str, Any]:
            self.calls += 1
            self.response = response
            return {
                "access_token": "FAKE_ACCESS",
                "id_token_claims": {
                    "tid": config.tenant_id,
                    "preferred_username": "admin@example.onmicrosoft.com",
                },
            }

    client = Client()
    monkeypatch.setattr(store, "_client", lambda _persona: (client, Cache()))
    monkeypatch.setattr(login_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    return store, client


def test_manual_paste_valid_redirect(
    setup: tuple[TokenStore, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, client = setup
    monkeypatch.setattr(
        login_module.getpass,
        "getpass",
        lambda _prompt: "http://localhost/?state=FAKE_STATE&code=FAKE_CODE",
    )
    login_module.manual_login(store, "admin", ["User.Read"])
    output = capsys.readouterr()
    assert "https://example.test/authorize?state=FAKE_STATE" in output.out
    assert "private window" in output.out and "fail to load" in output.out
    assert "FAKE_CODE" not in output.out + output.err
    assert "FAKE_ACCESS" not in output.out + output.err
    assert client.response == {"state": "FAKE_STATE", "code": "FAKE_CODE"}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "address, reason",
    [
        ("http://localhost/?state=WRONG&code=FAKE_CODE", "state mismatch"),
        (
            "http://localhost/?state=FAKE_STATE&error=invalid_grant&code=FAKE_CODE",
            "authorization error invalid_grant",
        ),
    ],
)
def test_manual_paste_refuses_safely(
    setup: tuple[TokenStore, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    address: str,
    reason: str,
) -> None:
    store, client = setup
    monkeypatch.setattr(login_module.getpass, "getpass", lambda _prompt: address)
    with pytest.raises(ReauthRequired, match=reason) as error:
        login_module.manual_login(store, "admin", ["User.Read"])
    output = capsys.readouterr()
    assert "FAKE_CODE" not in str(error.value) + output.out + output.err
    assert client.calls == 0  # type: ignore[attr-defined]


def test_manual_login_refuses_non_tty_before_msal(
    setup: tuple[TokenStore, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, client = setup
    monkeypatch.setattr(login_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(ReauthRequired, match="manual login requires a TTY"):
        login_module.manual_login(store, "admin", ["User.Read"])
    assert client.calls == 0  # type: ignore[attr-defined]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "flag, expected", [([], "manual"), (["--manual"], "manual"), (["--access-flow"], "access")]
)
def test_cli_login_mode(
    setup: tuple[TokenStore, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: list[str],
    expected: str,
) -> None:
    store, _client = setup
    selected: list[str] = []
    monkeypatch.setattr(TeamsQaConfig, "load", lambda _path: store.config)
    monkeypatch.setattr(
        teams_cli, "manual_login", lambda _store, _persona, _scopes: selected.append("manual")
    )

    async def access(*_args: object) -> None:
        selected.append("access")

    monkeypatch.setattr(teams_cli, "login_with_access_flow", access)
    assert teams_cli.main(["login", "--persona", "admin", *flag]) == 0
    assert selected == [expected]
    assert "sign-in complete" in capsys.readouterr().out
