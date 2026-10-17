"""`daimon.core.mcp_oauth.handshake`: minting the flow and preparing the redirect."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from daimon.core.config import McpOAuthClientSettings
from daimon.core.credential_requests import mint_request_token
from daimon.core.github_credentials import decrypt_token
from daimon.core.mcp_oauth.flow import generate_pkce
from daimon.core.mcp_oauth.handshake import (
    begin_mcp_oauth_flow,
    callback_url,
    prepare_authorization,
    start_url,
)
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.domain import CredentialRequestRow, McpOAuthFlowRow
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_ROOT = "https://daimon.example"
_MCP_URL = "https://mcp.notion.com/mcp"
_AS = {
    "issuer": "https://mcp.notion.com",
    "authorization_endpoint": "https://mcp.notion.com/authorize",
    "token_endpoint": "https://mcp.notion.com/token",
    "registration_endpoint": "https://mcp.notion.com/register",
    "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
}


async def _request(
    session: AsyncSession, *, kind: str = "mcp_oauth", mcp_server_url: str = _MCP_URL
) -> CredentialRequestRow:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    return await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind=kind,  # type: ignore[arg-type]  # the invalid-kind test needs a wrong value
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=account.id,
        target="notion",
        mcp_server_url=mcp_server_url,
        requester_platform_user_id="requester-1",
        channel_id="chan-1",
        expires_at=_NOW + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="daimon",
        requested_work=None,
    )


def test_start_and_callback_urls_hang_off_the_app_root() -> None:
    assert start_url(_ROOT + "/", state="a b") == f"{_ROOT}/oauth/mcp/start?state=a%20b"
    assert callback_url(_ROOT) == f"{_ROOT}/oauth/mcp/callback"


async def test_begin_flow_binds_the_requesters_identity_and_a_verifier(
    db_session: AsyncSession,
) -> None:
    request = await _request(db_session)
    flow = await begin_mcp_oauth_flow(
        db_session, request=request, app_root_url=_ROOT, now=_NOW, state="st_1"
    )
    assert (flow.tenant_id, flow.account_id, flow.agent_id) == (
        request.tenant_id,
        request.account_id,
        request.agent_id,
    ), "the grant must land in the requester's own vault, so the flow carries their identity"
    assert flow.redirect_uri == f"{_ROOT}/oauth/mcp/callback"
    assert len(flow.code_verifier) >= 43, "a PKCE verifier is minted per flow"
    assert flow.expires_at == _NOW + timedelta(minutes=10)


async def test_begin_flow_refuses_a_request_of_another_kind(db_session: AsyncSession) -> None:
    request = await _request(db_session, kind="mcp")
    with pytest.raises(ValueError, match="does not start an OAuth flow"):
        await begin_mcp_oauth_flow(db_session, request=request, app_root_url=_ROOT, now=_NOW)


async def test_prepare_authorization_registers_a_client_and_builds_the_redirect(
    db_session: AsyncSession,
) -> None:
    request = await _request(db_session)
    flow = await begin_mcp_oauth_flow(
        db_session, request=request, app_root_url=_ROOT, now=_NOW, code_verifier="v" * 64
    )
    registrations: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path == "/mcp":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.notion.com/.well-known/oauth-protected-resource"'
                },
            )
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.notion.com",
                    "authorization_servers": ["https://mcp.notion.com"],
                    "scopes_supported": ["default"],
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=_AS)
        if req.method == "POST" and path == "/register":
            registrations.append(json.loads(req.content))
            return httpx.Response(
                201,
                json={
                    "client_id": "cid",
                    "client_secret": "sec",
                    "token_endpoint_auth_method": "client_secret_post",
                },
            )
        return httpx.Response(404)

    fernet = make_fernet()
    prepared = await prepare_authorization(
        db_session,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        flow=flow,
        fernet=fernet,
        clients=[],
    )
    assert prepared is not None
    assert registrations[0]["redirect_uris"] == [f"{_ROOT}/oauth/mcp/callback"], (
        "the client is registered for daimon's callback"
    )
    query = parse_qs(urlparse(prepared.authorize_url).query)
    assert query["client_id"] == ["cid"] and query["state"] == [flow.state]
    assert query["code_challenge"] == [generate_pkce(verifier="v" * 64).code_challenge]
    assert query["scope"] == ["default"] and query["resource"] == ["https://mcp.notion.com"]

    saved = await flows_store.get_flow(db_session, state=flow.state)
    assert saved is not None and saved.token_endpoint == "https://mcp.notion.com/token"
    assert saved.client_secret_encrypted is not None
    assert decrypt_token(fernet, saved.client_secret_encrypted.encode()) == "sec", (
        "a confidential client's secret is stored encrypted, never in the clear"
    )


_EXAMPLE_MCP_URL = "https://mcp.example.com/mcp"
_EXAMPLE_CLIENT = McpOAuthClientSettings(
    authorization_server_host="login.example.com",
    client_id="app-id",
    client_secret=SecretStr("app-secret"),
    authorization_endpoint="https://login.example.com/tenant-1/authorize",
    token_endpoint="https://login.example.com/tenant-1/token",
    send_resource=False,
)


async def _prepare_with_clients(
    db_session: AsyncSession,
    *,
    authorization_server: str,
    metadata: dict[str, object],
    seen: list[httpx.Request],
) -> tuple[McpOAuthFlowRow, str]:
    """Run `prepare_authorization` with `_EXAMPLE_CLIENT` configured; return the saved row and URL.

    The MCP server names `authorization_server`, whose path-aware metadata is
    `metadata`; every request is recorded in `seen` and registration answers.
    """
    request = await _request(db_session, mcp_server_url=_EXAMPLE_MCP_URL)
    flow = await begin_mcp_oauth_flow(db_session, request=request, app_root_url=_ROOT, now=_NOW)
    issuer = urlparse(authorization_server)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "mcp.example.com" and req.url.path == "/mcp":
            return httpx.Response(401)
        if req.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                json={
                    "resource": _EXAMPLE_MCP_URL,
                    "authorization_servers": [authorization_server],
                    "scopes_supported": ["mcp.read"],
                },
            )
        if req.url.host == issuer.hostname and req.url.path == (
            f"{issuer.path}/.well-known/openid-configuration"
        ):
            return httpx.Response(200, json=metadata)
        if req.url.path == "/register":
            return httpx.Response(201, json={"client_id": "registered"})
        return httpx.Response(404)

    fernet = make_fernet()
    prepared = await prepare_authorization(
        db_session,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        flow=flow,
        fernet=fernet,
        clients=[_EXAMPLE_CLIENT],
    )
    assert prepared is not None, "a fresh flow always prepares"
    saved = await flows_store.get_flow(db_session, state=flow.state)
    assert saved is not None
    if saved.client_secret_encrypted is not None:
        assert decrypt_token(fernet, saved.client_secret_encrypted.encode()) == "app-secret", (
            "a configured secret is stored encrypted like a registered one"
        )
    return saved, prepared.authorize_url


async def test_prepare_authorization_uses_the_configured_client_for_its_host(
    db_session: AsyncSession,
) -> None:
    """No registration, overrides used, offline_access added, resource left out."""
    seen: list[httpx.Request] = []
    saved, authorize_url = await _prepare_with_clients(
        db_session,
        authorization_server="https://login.example.com/organizations/v2.0",
        metadata={
            "issuer": "https://login.example.com/organizations/v2.0",
            "authorization_endpoint": "https://login.example.com/organizations/authorize",
            "token_endpoint": "https://login.example.com/organizations/token",
            "scopes_supported": ["openid", "offline_access"],
        },
        seen=seen,
    )
    assert [r.url.path for r in seen if r.method == "POST"] == ["/mcp"], (
        "a configured client is used as is, never registered"
    )
    assert authorize_url.startswith("https://login.example.com/tenant-1/authorize?"), (
        "the authorization endpoint override replaces the discovered one"
    )
    query = parse_qs(urlparse(authorize_url).query)
    assert query["client_id"] == ["app-id"]
    assert query["scope"] == ["mcp.read offline_access"], (
        "offline_access is requested because the server advertises it"
    )
    assert "resource" not in query, "send_resource=false leaves resource off the authorize URL"
    assert (saved.client_id, saved.token_endpoint_auth_method) == ("app-id", "client_secret_post")
    assert saved.token_endpoint == "https://login.example.com/tenant-1/token", (
        "the code exchange and refresh use the token endpoint override"
    )
    assert saved.resource is None, "nor is resource stored for the exchange or the refresh"
    assert saved.client_secret_encrypted is not None


async def test_prepare_authorization_registers_when_the_metadata_host_matches_no_client(
    db_session: AsyncSession,
) -> None:
    """Matching uses the host the metadata came from, not the issuer it claims."""
    seen: list[httpx.Request] = []
    saved, authorize_url = await _prepare_with_clients(
        db_session,
        authorization_server="https://auth.example.org/tenant",
        metadata={
            "issuer": "https://login.example.com/tenant",
            "authorization_endpoint": "https://auth.example.org/authorize",
            "token_endpoint": "https://auth.example.org/token",
            "registration_endpoint": "https://auth.example.org/register",
            "scopes_supported": ["openid"],
        },
        seen=seen,
    )
    assert [r.url.host for r in seen if r.url.path == "/register"] == ["auth.example.org"], (
        "an unmatched server still gets a dynamically registered client"
    )
    query = parse_qs(urlparse(authorize_url).query)
    assert query["client_id"] == ["registered"] and query["scope"] == ["mcp.read"], (
        "offline_access is not requested when the server does not advertise it"
    )
    assert query["resource"] == [_EXAMPLE_MCP_URL], "resource is sent as before"
    assert saved.token_endpoint == "https://auth.example.org/token"
    assert saved.client_secret_encrypted is None, "the configured secret never leaves for it"
