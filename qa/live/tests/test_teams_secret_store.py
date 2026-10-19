"""Offline contracts for shared Teams QA token caches and refresh."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.exceptions import NotFound, PermissionDenied
from google.cloud import secretmanager

from qa.live.teams import __main__ as cli
from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.secret_store import SecretManagerCacheBackend
from qa.live.teams.tokens import ReauthRequired, TokenStore

SENTINEL = "PRIVATE_CACHE_PAYLOAD"


class FakeSecrets:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.versions: list[SimpleNamespace] = []
        self.destroyed: list[str] = []
        self.deny_prune = False
        self.deny_destroy = False

    def access_secret_version(self, *, request: dict[str, str]) -> SimpleNamespace:
        name = request["name"].removesuffix("/versions/latest")
        if name not in self.payloads:
            raise NotFound("missing")
        return SimpleNamespace(payload=SimpleNamespace(data=self.payloads[name]))

    def add_secret_version(self, *, request: dict[str, Any]) -> SimpleNamespace:
        name = request["parent"]
        self.payloads[name] = request["payload"]["data"]
        version = SimpleNamespace(
            name=f"{name}/versions/{len(self.versions) + 1}",
            state=secretmanager.SecretVersion.State.ENABLED,
        )
        self.versions.append(version)
        return version

    def list_secret_versions(self, *, request: dict[str, str]) -> list[SimpleNamespace]:
        if self.deny_prune:
            raise PermissionDenied("list denied")
        return [v for v in self.versions if v.name.startswith(request["parent"] + "/")]

    def destroy_secret_version(self, *, request: dict[str, str]) -> None:
        if self.deny_prune or self.deny_destroy:
            raise PermissionDenied("destroy denied")
        self.destroyed.append(request["name"])
        for version in self.versions:
            if version.name == request["name"]:
                version.state = secretmanager.SecretVersion.State.DESTROYED


def config_source() -> dict[str, Any]:
    source = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    source["token_store"] = {
        "kind": "secret_manager",
        "project": "qa-project",
        "secret_prefix": "teams-qa",
    }
    return source


def test_cache_versions_and_prune() -> None:
    api = FakeSecrets()
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    assert backend.read("admin") is None
    for _ in range(5):
        backend.write("admin", SENTINEL)
    assert backend.read("admin") == SENTINEL
    assert api.destroyed == [version.name for version in api.versions[:2]]
    api.deny_prune = True
    backend.write("admin", SENTINEL)
    assert len(api.versions) == 6
    api.deny_prune = False
    api.deny_destroy = True
    backend.write("admin", SENTINEL)
    assert len(api.versions) == 7
    with pytest.raises(ValueError, match="shared Secret Manager"):
        backend.delete("admin")


def test_config_secret_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeSecrets()
    config_name = "projects/qa-project/secrets/teams-qa-config"
    api.payloads[config_name] = json.dumps(config_source()).encode()
    monkeypatch.setattr("qa.live.teams.secret_store.client", lambda: api)
    monkeypatch.setenv("DAIMON_TEAMS_QA_CONFIG", "/nonexistent/teams-qa.json")
    config = TeamsQaConfig.load(config_secret=config_name)
    assert config.token_dir is None
    store = TokenStore(config, SecretManagerCacheBackend("qa-project", "teams-qa", api))
    for claims, reason in (
        ({"tid": "wrong"}, "tenant mismatch"),
        ({"tid": config.tenant_id, "upn": "wrong"}, "account mismatch"),
        (
            {"tid": config.tenant_id, "upn": config.personas["admin"].upn, "oid": "wrong"},
            "object mismatch",
        ),
    ):
        config.personas["admin"].expected_oid = "expected"
        with pytest.raises(ReauthRequired, match=reason) as error:
            store.validate("admin", {"access_token": SENTINEL, "id_token_claims": claims})
        assert SENTINEL not in str(error.value)
    with pytest.raises(ValueError, match="shared Secret Manager"):
        store.logout("admin")


def test_unchanged_cache_adds_no_version(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TeamsQaConfig.model_validate(config_source())
    api = FakeSecrets()
    store = TokenStore(config, SecretManagerCacheBackend("qa-project", "teams-qa", api))
    claim = {"tid": config.tenant_id, "upn": config.personas["admin"].upn}
    account = {"username": config.personas["admin"].upn, "realm": config.tenant_id}
    cache = SimpleNamespace(has_state_changed=False, serialize=lambda: SENTINEL)
    client = SimpleNamespace(
        get_accounts=lambda: [account],
        acquire_token_silent=lambda *_args, **_kw: {
            "access_token": "opaque",
            "id_token_claims": claim,
        },
    )
    monkeypatch.setattr(store, "_client", lambda _persona: (client, cache))
    store.acquire("admin", ["User.Read"])
    assert not api.versions
    cache.has_state_changed = True
    store.acquire("admin", ["User.Read"])
    assert len(api.versions) == 1
    assert store.version_added


def test_refresh_reports_and_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = TeamsQaConfig.model_validate(config_source())
    monkeypatch.setattr(TeamsQaConfig, "load", lambda *_args: config)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    class FakeStore:
        version_added = True

        def __init__(self, _config: TeamsQaConfig) -> None:
            pass

        def acquire(
            self, persona: str, scopes: list[str], *, force_refresh: bool
        ) -> SimpleNamespace:
            assert force_refresh and "Chat.ReadWrite" in scopes
            if persona == "member":
                raise ReauthRequired(persona, "account unavailable")
            return SimpleNamespace(value="opaque")

        def status(self, _persona: str) -> dict[str, str]:
            return {"expiry": "2026-10-19T00:00:00+00:00"}

    monkeypatch.setattr(cli, "TokenStore", FakeStore)
    assert (
        cli.main(["refresh", "--all", "--config-secret", "projects/qa-project/secrets/config"]) == 1
    )
    output = capsys.readouterr()
    lines = [json.loads(line) for line in output.out.splitlines()]
    assert lines == [
        {
            "persona": "admin",
            "ok": True,
            "version_added": True,
            "access_expiry": "2026-10-19T00:00:00+00:00",
        },
        {"persona": "member", "ok": False, "version_added": False, "access_expiry": None},
    ]
    assert "member: reauthentication required (account unavailable)" in output.err
    assert "::error title=Teams QA reauth::" in output.err
    assert "--config-secret <TEAMS_QA_CONFIG_SECRET>" in output.err
    assert "qa-project" not in output.err
    assert SENTINEL not in output.out + output.err
