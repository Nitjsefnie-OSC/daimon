"""/oauth/microsoft/start and /callback: PKCE, single-use state, the identity check."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.oauth_microsoft import build_oauth_microsoft_routes
from daimon.adapters.mcp.tools.microsoft._access import MicrosoftGraph
from daimon.core.config import MicrosoftAccountsSettings
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.mcp_oauth.flow import generate_pkce
from daimon.core.stores import microsoft_sign_ins as sign_ins
from daimon.core.stores import microsoft_user_tokens as tokens_store
from daimon.core.stores.domain import MicrosoftSignInRow
from daimon.testing.factories import make_account, make_tenant
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

ENTRA = "11111111-1111-1111-1111-111111111111"
OID = "22222222-2222-2222-2222-222222222222"
CONFIG = MicrosoftAccountsSettings(
    client_id="app-1",
    client_secret=SecretStr("app-secret"),
    tenant_id=ENTRA,
    authority=HttpUrl("https://login.example.com"),
)
FERNET = build_multifernet((Fernet.generate_key().decode(),))


def _id_token(**claims: object) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


class TokenEndpoint:
    def __init__(self, id_token: str) -> None:
        self.id_token = id_token
        self.forms: list[dict[str, list[str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.forms.append(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt-new",
                "expires_in": 3600,
                "scope": "User.Read Mail.Read",
                "id_token": self.id_token,
            },
        )


async def _app(
    sm: async_sessionmaker[AsyncSession], endpoint: TokenEndpoint
) -> tuple[httpx.AsyncClient, MicrosoftSignInRow]:
    graph = MicrosoftGraph(
        config=CONFIG,
        fernet=FERNET,
        session_factory=sm,
        http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(endpoint)),
    )
    start, callback = build_oauth_microsoft_routes(graph=graph, app_root_url="https://d.example")
    app = Starlette(
        routes=[
            Route("/oauth/microsoft/start", start),
            Route("/oauth/microsoft/callback", callback),
        ]
    )
    now = datetime.now(UTC)
    async with sm.begin() as session:
        tenant = await make_tenant(session, platform="teams", workspace_id=ENTRA)
        account = await make_account(session, tenant=tenant)
        row = await sign_ins.create_sign_in(
            session,
            state="state-1",
            tenant_id=tenant.id,
            account_id=account.id,
            user_id=OID,
            code_verifier="v" * 48,
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://d.example")
    return client, row


async def _stored(sm: async_sessionmaker[AsyncSession], row: MicrosoftSignInRow) -> str | None:
    async with sm() as session:
        token = await tokens_store.get_microsoft_user_token(
            session, tenant_id=row.tenant_id, user_id=OID
        )
    return None if token is None else decrypt_token(FERNET, token.encrypted_refresh_token)


async def test_start_redirects_to_the_tenant_with_the_bound_pkce_challenge(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, row = await _app(committing_sessionmaker, TokenEndpoint(""))
    response = await client.get("/oauth/microsoft/start", params={"state": row.state})
    location = urlsplit(response.headers["location"])
    query = parse_qs(location.query)
    assert response.status_code == 302, "a live link must redirect to sign-in"
    assert location.path == f"/{ENTRA}/oauth2/v2.0/authorize", "sign-in is tenant-specific"
    assert query["code_challenge"] == [generate_pkce(verifier=row.code_verifier).code_challenge], (
        "the challenge must match the verifier stored for this link"
    )
    assert query["redirect_uri"] == ["https://d.example/oauth/microsoft/callback"], (
        "the redirect must be the registered callback"
    )


async def test_callback_stores_the_token_once(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    endpoint = TokenEndpoint(_id_token(tid=ENTRA, aud="app-1", oid=OID))
    client, row = await _app(committing_sessionmaker, endpoint)
    params = {"state": row.state, "code": "c1"}
    response = await client.get("/oauth/microsoft/callback", params=params)
    assert response.status_code == 200, "the right account must connect"
    assert await _stored(committing_sessionmaker, row) == "rt-new", "the refresh token is stored"
    assert endpoint.forms[0]["code_verifier"] == [row.code_verifier], "PKCE verifier must be sent"
    replay = await client.get("/oauth/microsoft/callback", params=params)
    assert replay.status_code == 400, "a replayed state must be refused"
    assert len(endpoint.forms) == 1, "a replay must not reach the token endpoint"


@pytest.mark.parametrize(
    "claims",
    [
        {"tid": ENTRA, "aud": "app-1", "oid": "44444444-4444-4444-4444-444444444444"},
        {"tid": "33333333-3333-3333-3333-333333333333", "aud": "app-1", "oid": OID},
    ],
    ids=["another person", "another tenant"],
)
async def test_callback_refuses_another_account(
    committing_sessionmaker: async_sessionmaker[AsyncSession], claims: dict[str, str]
) -> None:
    client, row = await _app(committing_sessionmaker, TokenEndpoint(_id_token(**claims)))
    response = await client.get(
        "/oauth/microsoft/callback", params={"state": row.state, "code": "c1"}
    )
    assert response.status_code == 403, "the wrong account must be refused"
    assert await _stored(committing_sessionmaker, row) is None, "nothing may be stored"


async def test_cancelled_sign_in_spends_the_link(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    endpoint = TokenEndpoint("")
    client, row = await _app(committing_sessionmaker, endpoint)
    response = await client.get(
        "/oauth/microsoft/callback", params={"state": row.state, "error": "access_denied"}
    )
    assert response.status_code == 200 and "cancelled" in response.text, "say it was cancelled"
    again = await client.get("/oauth/microsoft/start", params={"state": row.state})
    assert again.status_code == 400, "a cancelled link must not start again"
    assert not endpoint.forms, "a cancelled sign-in must not exchange anything"


async def test_unknown_state_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = await _app(committing_sessionmaker, TokenEndpoint(""))
    start = await client.get("/oauth/microsoft/start", params={"state": "forged"})
    callback = await client.get(
        "/oauth/microsoft/callback", params={"state": "forged", "code": "c"}
    )
    assert (start.status_code, callback.status_code) == (400, 400), "a forged state is refused"
