"""Mocked permission tests for a staged Teams file-consent offer."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from anthropic.types.beta import FileMetadata
from daimon.core import output_delivery
from daimon.testing import build_fake_anthropic, list_response
from daimon.testing.ma import MARouter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    build_teams_runtime,
    finish_turns,
    make_inbound,
    post_activity,
    running_service,
)
from .personas import PERSONAS, Persona, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
DATA = Path(__file__).parent / "data/activities"


def _payload(action: Literal["accept", "decline"], actor: Persona) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((DATA / f"file_consent_{action}.json").read_text())
    sender = payload["from"]
    sender["id"] = f"29:{actor.user_id}"
    sender["aadObjectId"] = actor.user_id
    if actor.name == "external":
        sender["tenantId"] = actor.tenant_id
    return payload


@pytest.mark.parametrize("action", ("accept", "decline"))
@pytest.mark.parametrize("actor", PERSONAS, ids=lambda persona: persona.name)
async def test_file_consent_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    action: Literal["accept", "decline"],
    actor: Persona,
) -> None:
    """Mocked permission tests keep a staged offer bound to its requester."""
    monkeypatch.setattr(output_delivery, "_POLL_DELAYS_S", (0.0,))
    uploads: list[httpx.Request] = []
    deletes: list[str] = []
    listing = FileMetadata(
        id="file_1",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        filename="file_example.txt",
        mime_type="text/plain",
        size_bytes=4,
        type="file",
        downloadable=True,
    )
    router = MARouter()
    router.add(
        "GET", r"/v1/files$", lambda _r, _m: list_response([listing.model_dump(mode="json")])
    )
    router.add(
        "GET", r"/v1/files/([^/]+)/content", lambda _r, _m: httpx.Response(200, content=b"text")
    )

    def remove(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    def upload(request: httpx.Request) -> httpx.Response:
        uploads.append(request)
        return httpx.Response(201, json={"id": "item-1"})

    router.add("DELETE", r"/v1/files/([^/]+)", remove)
    fake = PersonaTeamsApiFake(active=PERSONAS[0])
    runtime = build_teams_runtime(
        db_session_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upload)),
    )
    async with running_service(runtime, fake) as service:
        await service.turns.outputs.sweep(make_inbound(), "sesn_1")
        offer = next(
            request.body for request in fake.activity_requests if request.body.get("attachments")
        )
        attachment: Any = offer["attachments"]
        click = _payload(action, actor)
        click["value"]["context"] = attachment[0]["content"][f"{action}Context"]
        result = await post_activity(service, click)
        await finish_turns(service.turns)
        if actor.name != "admin":
            assert uploads == [] and deletes == [], result
            requester = _payload(action, PERSONAS[0])
            requester["value"]["context"] = click["value"]["context"]
            await post_activity(service, requester)
            await finish_turns(service.turns)
    assert deletes == ["file_1"]
    assert len(uploads) == (1 if action == "accept" else 0)
