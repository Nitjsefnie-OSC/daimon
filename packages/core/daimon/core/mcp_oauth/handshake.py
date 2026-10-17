"""The two halves of an MCP OAuth handshake daimon drives itself.

`begin_mcp_oauth_flow` runs in the chat adapter on the requester's click: it
spends nothing but mints the flow row (state + PKCE verifier) whose start
link only that person sees. `prepare_authorization` runs on the mcp process
when the link is opened: discovery, a client (registered by hand for that
authorization server, else dynamically), and the authorize URL to redirect
to. Both take their clock, randomness and clients as arguments.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import quote, urlparse

import httpx
from cryptography.fernet import MultiFernet
from daimon.core.config import McpOAuthClientSettings
from daimon.core.github_credentials import encrypt_token
from daimon.core.mcp_oauth.discovery import discover_authorization_server, probe_mcp_server
from daimon.core.mcp_oauth.flow import (
    build_authorization_url,
    generate_pkce,
    register_client,
    requested_scope,
)
from daimon.core.mcp_oauth.models import ClientRegistration
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.domain import CredentialRequestRow, McpOAuthFlowRow
from sqlalchemy.ext.asyncio import AsyncSession

#: A person has this long between clicking the card and finishing sign-in.
FLOW_TTL: Final[timedelta] = timedelta(minutes=10)
CLIENT_NAME: Final[str] = "daimon"


def invite_copy(*, server_name: str, agent_name: str) -> str:
    """The ephemeral text next to the sign-in button, identical on every platform."""
    return (
        f"Sign in to connect {server_name} to {agent_name} with your account. "
        "This link is yours alone and works for ten minutes; nobody else can use "
        "the connection it creates."
    )


INVITE_BUTTON_LABEL: Final[str] = "Open sign-in"


def start_url(app_root_url: str, *, state: str) -> str:
    return f"{app_root_url.rstrip('/')}/oauth/mcp/start?state={quote(state, safe='')}"


def callback_url(app_root_url: str) -> str:
    return f"{app_root_url.rstrip('/')}/oauth/mcp/callback"


async def begin_mcp_oauth_flow(
    session: AsyncSession,
    *,
    request: CredentialRequestRow,
    app_root_url: str,
    now: datetime,
    state: str | None = None,
    code_verifier: str | None = None,
) -> McpOAuthFlowRow:
    """Mint the flow row for a consumed `mcp_oauth` request; return it.

    The caller has already spent the request row for its requester, so the
    identity on the flow (tenant, account, agent) is the requester's and the
    credential the callback writes lands in that person's vault only.
    """
    if request.kind != "mcp_oauth":
        raise ValueError(f"kind={request.kind!r} does not start an OAuth flow")
    if request.mcp_server_url is None:
        raise ValueError("an mcp_oauth request must name its server URL")
    pkce = generate_pkce(verifier=code_verifier)
    return await flows_store.create_flow(
        session,
        state=state if state is not None else secrets.token_urlsafe(32),
        request_token=request.token,
        tenant_id=request.tenant_id,
        account_id=request.account_id,
        agent_id=request.agent_id,
        server_name=request.target,
        mcp_server_url=request.mcp_server_url,
        redirect_uri=callback_url(app_root_url),
        code_verifier=pkce.code_verifier,
        expires_at=now + FLOW_TTL,
    )


@dataclass(frozen=True, slots=True)
class PreparedAuthorization:
    flow: McpOAuthFlowRow
    authorize_url: str


def match_preregistered_client(
    clients: Sequence[McpOAuthClientSettings], *, authorization_server_url: str
) -> McpOAuthClientSettings | None:
    """The configured client for the host discovery fetched the metadata from.

    Matched on that host, not on the `issuer` the document claims: a hostile
    MCP server could serve metadata naming a configured issuer with its own
    token endpoint, and the client secret would go there.
    """
    host = (urlparse(authorization_server_url).hostname or "").rstrip(".").lower()
    return next((c for c in clients if c.authorization_server_host == host), None)


def _reuse_registered(flow: McpOAuthFlowRow) -> PreparedAuthorization:
    """The authorize URL for a flow whose client is already on the row."""
    assert flow.client_id is not None and flow.authorization_endpoint is not None
    return PreparedAuthorization(
        flow=flow,
        authorize_url=build_authorization_url(
            flow.authorization_endpoint,
            client_id=flow.client_id,
            redirect_uri=flow.redirect_uri,
            state=flow.state,
            code_challenge=generate_pkce(verifier=flow.code_verifier).code_challenge,
            scope=flow.scope,
            resource=flow.resource,
        ),
    )


async def prepare_authorization(
    session: AsyncSession,
    http: httpx.AsyncClient,
    *,
    flow: McpOAuthFlowRow,
    fernet: MultiFernet,
    clients: Sequence[McpOAuthClientSettings],
) -> PreparedAuthorization | None:
    """Discover the server's authorization server, pick a client, build the URL.

    A client in `clients` configured for the authorization server's host is
    used as is, with its endpoint overrides and its choice about `resource`;
    any other server gets a dynamically registered client.

    A link opened twice reuses the client the first open registered, so the
    code the provider issues is always exchanged as the client that asked for
    it. Returns None when the flow was spent in between; the caller shows the
    expired page.
    """
    if flow.client_id is not None:
        return _reuse_registered(flow)
    probe = await probe_mcp_server(http, mcp_server_url=flow.mcp_server_url)
    discovery = await discover_authorization_server(
        http,
        mcp_server_url=flow.mcp_server_url,
        resource_metadata_url=probe.resource_metadata_url,
    )
    metadata = discovery.authorization_server
    scope = requested_scope(
        discovery.resource.scopes_supported if discovery.resource else None,
        metadata.scopes_supported,
    )
    resource = discovery.resource.resource if discovery.resource else None
    preregistered = match_preregistered_client(
        clients, authorization_server_url=discovery.authorization_server_url
    )
    if preregistered is None:
        client = await register_client(
            http, metadata, redirect_uri=flow.redirect_uri, client_name=CLIENT_NAME, scope=scope
        )
    else:
        secret = preregistered.client_secret
        client = ClientRegistration(
            client_id=preregistered.client_id,
            client_secret=secret.get_secret_value() if secret is not None else None,
            token_endpoint_auth_method=preregistered.token_endpoint_auth_method,
        )
        metadata = metadata.model_copy(
            update={
                "authorization_endpoint": preregistered.authorization_endpoint
                or metadata.authorization_endpoint,
                "token_endpoint": preregistered.token_endpoint or metadata.token_endpoint,
            }
        )
        if not preregistered.send_resource:
            resource = None
    saved = await flows_store.save_flow_client(
        session,
        state=flow.state,
        client_id=client.client_id,
        client_secret_encrypted=(
            encrypt_token(fernet, client.client_secret).decode()
            if client.client_secret is not None
            else None
        ),
        token_endpoint_auth_method=client.token_endpoint_auth_method,
        token_endpoint=metadata.token_endpoint,
        authorization_endpoint=metadata.authorization_endpoint,
        resource=resource,
        scope=scope,
    )
    if saved is None:
        # Lost a race with a concurrent open of the same link, or the flow was
        # spent: re-read and reuse whatever landed first.
        current = await flows_store.get_flow(session, state=flow.state)
        if current is None or current.used_at is not None or current.client_id is None:
            return None
        return _reuse_registered(current)
    authorize_url = build_authorization_url(
        metadata.authorization_endpoint,
        client_id=client.client_id,
        redirect_uri=flow.redirect_uri,
        state=flow.state,
        code_challenge=generate_pkce(verifier=flow.code_verifier).code_challenge,
        scope=scope,
        resource=resource,
    )
    return PreparedAuthorization(flow=saved, authorize_url=authorize_url)
