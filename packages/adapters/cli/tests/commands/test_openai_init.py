"""`daimon openai init` provisions real OpenAI resources and writes one
runtime-manifest row that the mux admission path can actually load."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from daimon.adapters.cli.commands.openai import openai_init_command
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.testing.isolated_sdk import build_async_openai
from mux.drivers.openai.transport import SDKTransport

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def fake_transport_factory(calls: list[httpx.Request]):
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content or b"{}")
        if request.url.path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "id": "agent-123",
                    "model": body["model"],
                    "name": body.get("name", ""),
                    "metadata": body.get("metadata", {}),
                    "created_at": 1700000000,
                },
            )
        if request.url.path == "/v1/agents/environments/templates":
            return httpx.Response(
                200, json={"id": "env-123", "name": body.get("name", ""), "created_at": 1700000000}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    def factory(*, api_key: str, project: str):
        del api_key, project
        return SDKTransport(build_async_openai(transport=httpx.MockTransport(handle)))

    return factory


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "manifest.json"


def test_init_creates_agent_and_environment_and_writes_one_row(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake_transport_factory(calls)
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
    )

    rows = json.loads(manifest_path.read_text())
    assert len(rows) == 1
    deployment = OpenAIDeployment.model_validate(rows[0])
    assert deployment.agent_id == "agent-123"
    assert deployment.environment_id == "env-123"
    assert deployment.platform == "*"
    assert deployment.channel_id == "*"
    assert deployment.account_id == "*"
    assert deployment.tenant_id == str(TENANT_ID)
    assert deployment.api_key_env == "DAIMON_CHANNEL_OPENAI_KEY"
    # Never the key value itself, anywhere in the written file.
    assert "sk-test-not-a-real-key" not in manifest_path.read_text()
    assert [c.url.path for c in calls] == ["/v1/agents", "/v1/agents/environments/templates"]


def test_init_is_idempotent_and_does_not_reprovision(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake_transport_factory(calls)
    )

    for _ in range(2):
        openai_init_command(
            tenant_id=TENANT_ID,
            project="proj_123",
            model="gpt-6-luna",
            manifest=manifest_path,
        )

    rows = json.loads(manifest_path.read_text())
    assert len(rows) == 1
    # The second call reused the existing row instead of calling OpenAI again.
    assert len(calls) == 2


def test_init_conversation_only_profile_skips_environment(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake_transport_factory(calls)
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
        profile="openai.conversation_only",
    )

    rows = json.loads(manifest_path.read_text())
    deployment = OpenAIDeployment.model_validate(rows[0])
    assert deployment.environment_id is None
    assert [c.url.path for c in calls] == ["/v1/agents"]
