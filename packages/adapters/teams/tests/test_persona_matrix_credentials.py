"""Mocked permission tests for Teams credential and GitHub request dialogs."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest
from daimon.adapters.teams.credential_requests import SUBMIT
from daimon.core.credential_requests import build_skill_repo_target
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.posted_controls.teams_card import CREDENTIAL_DIALOG
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing import build_fake_anthropic, ma_agent
from daimon.testing.ma import MARouter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ADMIN_TEAM,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    build_teams_runtime,
    make_invoke,
    post_activity,
    running_service,
)
from .personas import PERSONAS, OwnerGraphFake, Persona, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
AGENT = "agt_persona"


def _invoke(kind: Literal["open", "submit"], token: str, persona: Persona) -> dict[str, object]:
    if kind == "open":
        data = {"msteams": {"type": "task/fetch"}, "dialog_id": CREDENTIAL_DIALOG, "token": token}
        payload = make_invoke("task/fetch", {"data": data}, user=persona.user_id)
    else:
        data = {"action": SUBMIT, "token": token, "secret": " "}
        payload = make_invoke("task/submit", {"data": data}, user=persona.user_id)
    if persona.name == "external":
        sender = payload["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = persona.tenant_id
    return payload


@pytest.mark.parametrize("kind", ("env", "repo"))
@pytest.mark.parametrize("actor", PERSONAS, ids=lambda persona: persona.name)
async def test_credential_request_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession],
    kind: Literal["env", "repo"],
    actor: Persona,
) -> None:
    """Mocked permission tests use Daimon's issuer and both registered dialog routes."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    requester = PERSONAS[0]
    async with db_session_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=requester.user_id
        )
        row = await create_credential_request(
            session,
            token=uuid.uuid4().hex,
            kind=kind,
            tenant_id=TENANT,
            agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id=AGENT),
            account_id=principal.account_id,
            target="API_KEY"
            if kind == "env"
            else build_skill_repo_target("https://github.com/o/r", "main", ""),
            mcp_server_url=None,
            requester_platform_user_id=requester.user_id,
            channel_id=CONVERSATION_ID,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=AGENT,
            target_name="daimon",
            requested_work="synthetic follow-up",
            platform="teams",
            origin_thread_id=CONVERSATION_ID,
            posted_message_id="m-7",
        )
    router = MARouter()
    agent = ma_agent(id=AGENT, name="daimon", tenant_id=TENANT)
    router.add_agent_list(agent)
    router.add_agent(agent)
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=actor)
    settings = build_teams_runtime(db_session_factory).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )
    async with running_service(runtime, fake) as service:
        opened = await post_activity(service, _invoke("open", row.token, actor))
        submitted = await post_activity(service, _invoke("submit", row.token, actor))
    async with db_session_factory() as session:
        stored = await peek_credential_request(session, token=row.token)
    assert stored is not None and stored.used_at is None
    if actor.name == "admin":
        assert opened["task"]["type"] == "continue", opened
        assert "cannot be empty" in json.dumps(submitted), submitted
    else:
        assert opened["task"]["type"] == "message", opened
        assert submitted["task"]["type"] == "message", submitted
        assert "cannot be empty" not in json.dumps(submitted), submitted
