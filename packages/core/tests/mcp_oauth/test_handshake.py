"""`daimon.core.mcp_oauth.handshake`: minting the flow and preparing the redirect."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import MultiFernet
from daimon.core.config import McpOAuthClientSettings
from daimon.core.credential_requests import mint_request_token
from daimon.core.github_credentials import decrypt_token
from daimon.core.mcp_oauth.flow import McpOAuthFlowError, generate_pkce
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
_EXAMPLE_ISSUER = "https://login.example.com/organizations/v2.0"
_EXAMPLE_METADATA: dict[str, object] = {
    "issuer": _EXAMPLE_ISSUER,
    "authorization_endpoint": "https://login.example.com/organizations/authorize",
    "token_endpoint": "https://login.example.com/organizations/token",
    "scopes_supported": ["openid", "offline_access"],
}
_EXAMPLE_CLIENT = McpOAuthClientSettings(
    authorization_server_host="login.example.com",
    mcp_servers=[_EXAMPLE_MCP_URL],
    client_id="app-id",
    client_secret=SecretStr("app-secret"),
    authorization_endpoint="https://login.example.com/tenant-1/authorize",
    token_endpoint="https://login.example.com/tenant-1/token",
    send_resource=False,
)
_PLAIN_CLIENT = McpOAuthClientSettings(
    authorization_server_host="login.example.com",
    mcp_servers=[_EXAMPLE_MCP_URL],
    client_id="app-id",
    client_secret=SecretStr("app-secret"),
)


async def _prepare_with_clients(
    db_session: AsyncSession,
    *,
    clients: list[McpOAuthClientSettings],
    metadata: dict[str, object],
    mcp_server_url: str = _EXAMPLE_MCP_URL,
    authorization_server: str = _EXAMPLE_ISSUER,
    resource_scopes: list[str] | None = None,
    seen: list[httpx.Request] | None = None,
) -> tuple[McpOAuthFlowRow, str, MultiFernet]:
    """Run `prepare_authorization` against one MCP server; return the saved row, URL and key.

    The MCP server names `authorization_server`, whose metadata is `metadata`,
    and publishes `resource_scopes` (default `["mcp.read"]`). Registration
    answers wherever it is offered; every request is recorded in `seen`.
    """
    request = await _request(db_session, mcp_server_url=mcp_server_url)
    flow = await begin_mcp_oauth_flow(db_session, request=request, app_root_url=_ROOT, now=_NOW)
    issuer = urlparse(authorization_server)
    issuer_host = (issuer.hostname or "").rstrip(".")
    published = ["mcp.read"] if resource_scopes is None else resource_scopes

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        path = req.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            body: dict[str, object] = {
                "resource": mcp_server_url,
                "authorization_servers": [authorization_server],
            }
            if published:
                body["scopes_supported"] = published
            return httpx.Response(200, json=body)
        if req.url.host.rstrip(".") == issuer_host and path == (
            f"{issuer.path}/.well-known/openid-configuration"
        ):
            return httpx.Response(200, json=metadata)
        if req.method == "POST" and path == "/register":
            return httpx.Response(201, json={"client_id": "registered"})
        if req.method == "POST":
            return httpx.Response(401)
        return httpx.Response(404)

    fernet = make_fernet()
    prepared = await prepare_authorization(
        db_session,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        flow=flow,
        fernet=fernet,
        clients=clients,
    )
    assert prepared is not None, "a fresh flow always prepares"
    saved = await flows_store.get_flow(db_session, state=flow.state)
    assert saved is not None, "the prepared flow is still on record"
    return saved, prepared.authorize_url, fernet


async def test_prepare_authorization_uses_the_configured_client_for_a_listed_server(
    db_session: AsyncSession,
) -> None:
    """No registration, overrides used, offline_access added, resource left out."""
    seen: list[httpx.Request] = []
    saved, authorize_url, fernet = await _prepare_with_clients(
        db_session, clients=[_EXAMPLE_CLIENT], metadata=_EXAMPLE_METADATA, seen=seen
    )
    assert [r.url.path for r in seen if r.method == "POST"] == ["/mcp"], (
        "a configured client is used as is, never registered"
    )
    assert authorize_url.startswith("https://login.example.com/tenant-1/authorize?"), (
        "the authorization endpoint override replaces the discovered one"
    )
    query = parse_qs(urlparse(authorize_url).query)
    assert query["client_id"] == ["app-id"], "the configured client id signs the person in"
    assert query["scope"] == ["mcp.read offline_access"], (
        "a configured client asks for offline_access because the server advertises it"
    )
    assert "resource" not in query, "send_resource=false leaves resource off the authorize URL"
    assert (saved.client_id, saved.token_endpoint_auth_method) == (
        "app-id",
        "client_secret_post",
    ), "the flow row carries the configured client for the exchange"
    assert saved.token_endpoint == "https://login.example.com/tenant-1/token", (
        "the code exchange and refresh use the token endpoint override"
    )
    assert saved.resource is None, "nor is resource stored for the exchange or the refresh"
    assert saved.client_secret_encrypted is not None, "the configured secret is stored"
    assert decrypt_token(fernet, saved.client_secret_encrypted.encode()) == "app-secret", (
        "a configured secret is stored encrypted like a registered one"
    )


async def test_prepare_authorization_never_lends_the_client_to_an_unlisted_server(
    db_session: AsyncSession,
) -> None:
    """A hostile server naming the configured sign-in host gets no client of ours.

    It publishes metadata pointing at the configured host and scopes of its
    choosing; signing in through the deployment's app would hand it a token
    for them. It falls back to registration, which that host does not offer.
    """
    seen: list[httpx.Request] = []
    with pytest.raises(McpOAuthFlowError, match="no dynamic client registration"):
        await _prepare_with_clients(
            db_session,
            clients=[_PLAIN_CLIENT],
            metadata=_EXAMPLE_METADATA,
            mcp_server_url="https://evil.example.net/mcp",
            resource_scopes=["mail.read", "files.read.all"],
            seen=seen,
        )
    assert not any("app-id" in str(r.url) for r in seen), (
        "the configured client id never leaves for an unlisted server"
    )


async def test_prepare_authorization_registers_when_the_metadata_host_matches_no_client(
    db_session: AsyncSession,
) -> None:
    """Matching uses the host the metadata came from, not the issuer it claims."""
    seen: list[httpx.Request] = []
    saved, authorize_url, _ = await _prepare_with_clients(
        db_session,
        clients=[_EXAMPLE_CLIENT],
        authorization_server="https://auth.example.org/tenant",
        metadata={
            "issuer": "https://login.example.com/tenant",
            "authorization_endpoint": "https://auth.example.org/authorize",
            "token_endpoint": "https://auth.example.org/token",
            "registration_endpoint": "https://auth.example.org/register",
            "scopes_supported": ["openid", "offline_access"],
        },
        seen=seen,
    )
    assert [r.url.host for r in seen if r.url.path == "/register"] == ["auth.example.org"], (
        "a listed server on another host still gets a dynamically registered client"
    )
    query = parse_qs(urlparse(authorize_url).query)
    assert query["client_id"] == ["registered"], "the registered client signs the person in"
    assert query["scope"] == ["mcp.read"], (
        "a registered client asks for exactly what it did before, without offline_access"
    )
    assert query["resource"] == [_EXAMPLE_MCP_URL], "resource is sent as before"
    assert saved.token_endpoint == "https://auth.example.org/token", "discovered endpoint used"
    assert saved.client_secret_encrypted is None, "the configured secret never leaves for it"


@pytest.mark.parametrize(
    ("mcp_server_url", "authorization_server", "expected_client"),
    [
        ("https://MCP.Example.com/mcp/", "https://LOGIN.Example.com./organizations/v2.0", "app-id"),
        ("https://mcp.example.com:443/mcp", _EXAMPLE_ISSUER, "app-id"),
        ("https://mcp.example.com/mcp", "https://login.example.com:8443/organizations/v2.0", None),
        ("https://mcp.example.com:8443/mcp", _EXAMPLE_ISSUER, None),
        ("https://mcp.example.com/mcp/other", _EXAMPLE_ISSUER, None),
    ],
)
async def test_prepare_authorization_matches_servers_and_hosts_in_normalised_form(
    db_session: AsyncSession,
    mcp_server_url: str,
    authorization_server: str,
    expected_client: str | None,
) -> None:
    """Case, a trailing dot and a default port match; another port or a longer path do not."""
    metadata = _EXAMPLE_METADATA | {"registration_endpoint": "https://login.example.com/register"}
    _, authorize_url, _ = await _prepare_with_clients(
        db_session,
        clients=[_PLAIN_CLIENT],
        metadata=metadata,
        mcp_server_url=mcp_server_url,
        authorization_server=authorization_server,
    )
    query = parse_qs(urlparse(authorize_url).query)
    assert query["client_id"] == [expected_client or "registered"], (
        f"{mcp_server_url} behind {authorization_server} should "
        f"{'use the configured client' if expected_client else 'register its own client'}"
    )


@pytest.mark.parametrize("endpoint", ["authorization_endpoint", "token_endpoint"])
async def test_prepare_authorization_refuses_a_discovered_endpoint_off_the_client_host(
    db_session: AsyncSession, endpoint: str
) -> None:
    """Without an override, the secret and the code only go to the configured host."""
    metadata = _EXAMPLE_METADATA | {endpoint: "https://collector.example.net/oauth"}
    with pytest.raises(McpOAuthFlowError, match=f"{endpoint} .* is not on login.example.com"):
        await _prepare_with_clients(db_session, clients=[_PLAIN_CLIENT], metadata=metadata)


async def test_prepare_authorization_uses_the_client_scope_when_the_server_publishes_none(
    db_session: AsyncSession,
) -> None:
    client = _PLAIN_CLIENT.model_copy(update={"scope": "api://mcp/.default"})
    saved, authorize_url, _ = await _prepare_with_clients(
        db_session, clients=[client], metadata=_EXAMPLE_METADATA, resource_scopes=[]
    )
    query = parse_qs(urlparse(authorize_url).query)
    assert query["scope"] == ["api://mcp/.default offline_access"], (
        "the configured scope stands in for the scopes the server does not publish"
    )
    assert saved.scope == "api://mcp/.default offline_access", (
        "the stored scope is what the refresh keeps offline_access from"
    )
    assert authorize_url.startswith("https://login.example.com/organizations/authorize?"), (
        "without overrides the discovered endpoints on the configured host are used"
    )
