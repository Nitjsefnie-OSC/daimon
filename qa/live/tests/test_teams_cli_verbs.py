"""Offline contracts for the CLI wiring of the Teams QA harness verbs."""

from __future__ import annotations

import json
from typing import Any

import pytest

from qa.live.teams import __main__ as cli
from qa.live.teams.config import TeamsQaConfig

CONFIG = {
    "tenant_id": "tenant",
    "client_id": "client",
    "team_id": "team",
    "channel_id": "channel",
    "team_display_name": "QA",
    "channel_display_name": "General",
    "bot_app_id": "bot",
    "bot_display_name": "Daimon",
    "token_store": {"kind": "secret_manager", "project": "qa-project"},
    "personas": {
        "admin": {"upn": "admin@example.test", "role": "admin", "vault_item": "a"},
        "member": {"upn": "member@example.test", "role": "member", "vault_item": "m"},
    },
}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    config = TeamsQaConfig.model_validate(CONFIG)
    monkeypatch.setattr(TeamsQaConfig, "load", lambda *_args: config)
    monkeypatch.setattr(cli, "TokenStore", lambda _config: object())
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    def verb(name: str, ok: bool = True) -> Any:
        async def run(_driver: object, *args: Any) -> dict[str, Any]:
            recorded.append((name, args))
            return {"ok": ok, "verb": name}

        return run

    monkeypatch.setattr(cli, "send", verb("send"))
    monkeypatch.setattr(cli, "read", verb("read"))
    monkeypatch.setattr(cli, "wait", verb("wait", ok=False))
    return recorded


def test_verbs_print_one_json_line_and_pass_arguments(
    calls: list[tuple[str, tuple[Any, ...]]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["send", "--persona", "admin", "--to", "chat", "routines"]) == 0
    assert cli.main(["read", "--persona", "member", "--from", "thread:42", "--since", "1h"]) == 0
    assert cli.main(["wait", "--persona", "admin", "--after", "7", "--timeout", "30"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["verb"] for line in lines] == ["send", "read", "wait"]
    assert calls == [
        ("send", ("admin", "chat", "routines")),
        ("read", ("member", "thread:42", "1h")),
        ("wait", ("admin", "7", 30.0, "chat")),
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["send", "--persona", "admin", "routines"],
        ["send", "--persona", "admin", "--to", "chat"],
        ["read", "--from", "chat"],
        ["wait", "--persona", "admin"],
        ["read", "--persona", "admin", "stray text"],
        ["status", "--persona", "admin", "--to", "chat"],
    ],
)
def test_verb_argument_errors(calls: list[tuple[str, tuple[Any, ...]]], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2
    assert calls == []


def test_unexpected_failure_stays_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_args: object) -> TeamsQaConfig:
        raise RuntimeError("Teams QA config secret unavailable")

    monkeypatch.setattr(TeamsQaConfig, "load", broken)
    assert cli.main(["read", "--persona", "admin"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "command_failed",
        "type": "RuntimeError",
    }
