"""`/oauth/microsoft/start` and `/oauth/microsoft/callback`: Connect Microsoft in the browser.

`connect_microsoft` minted a sign-in row and put its link on a card only the
person sees. `start` sends them to the tenant's Entra sign-in with PKCE.
`callback` spends the state (the atomic delete is the replay gate),
exchanges the code as a confidential client, and stores the refresh token
only if the account is in the configured tenant and its object id is the
Teams person's. Pages are static copy; nothing from the request or an
upstream error is interpolated into HTML.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import httpx
import structlog
from daimon.adapters.mcp.tools.microsoft._access import MicrosoftGraph
from daimon.adapters.mcp.web_icons import icon, platform_mark
from daimon.adapters.mcp.web_shell import render_page
from daimon.core.github_credentials import encrypt_token
from daimon.core.mcp_oauth.flow import generate_pkce
from daimon.core.microsoft_identity import (
    MicrosoftAppRejected,
    MicrosoftIdentityError,
    MicrosoftIdentityMismatch,
    build_authorize_url,
    exchange_code,
    verify_identity,
)
from daimon.core.stores import microsoft_sign_ins as sign_ins
from daimon.core.stores import microsoft_user_tokens as tokens_store
from daimon.core.stores.security_audit import append_event
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = structlog.get_logger(__name__)

Handler = Callable[[Request], Awaitable[Response]]
_Error = Literal[
    "expired", "declined", "blocked", "exchange_failed", "misconfigured", "wrong_account"
]
_COPY: dict[_Error, tuple[str, str, int]] = {
    "expired": (
        "This sign-in link has expired",
        "Links work once, for ten minutes. Ask Daimon to connect Microsoft again.",
        400,
    ),
    "declined": (
        "Sign-in was cancelled",
        "Nothing was connected. Ask Daimon again whenever you want to connect Microsoft.",
        200,
    ),
    "blocked": (
        "Microsoft did not allow the sign-in",
        "Your organisation may need to approve Daimon or allow this sign-in. Ask your IT "
        "administrator. Nothing was connected.",
        403,
    ),
    "misconfigured": (
        "Microsoft sign-in is not set up correctly",
        "Microsoft refused Daimon's app registration. Tell your Daimon administrator. "
        "Nothing was connected.",
        502,
    ),
    "exchange_failed": (
        "Sign-in did not complete",
        "Microsoft did not finish the sign-in. Ask Daimon to connect Microsoft again.",
        502,
    ),
    "wrong_account": (
        "That is not your Teams account",
        "Sign in with the same work account you use in Teams. Nothing was connected.",
        403,
    ),
}


def _error_page(kind: _Error) -> HTMLResponse:
    headline, text, status = _COPY[kind]
    body = (
        f'<div class="gh-status-icon">{icon("triangle-alert")}</div>'
        f"<h1>{html.escape(headline, quote=False)}</h1><p>{html.escape(text, quote=False)}</p>"
    )
    return render_page(
        title=headline, context="Microsoft", error=True, body_html=body, status=status
    )


def _success_page() -> HTMLResponse:
    body = (
        f'<div class="gh-status-icon">{icon("circle-check")}</div>'
        "<h1>Connected Microsoft 365</h1>"
        "<p>Daimon can work with your files, mail, calendar and notes from your next "
        "message. Disconnect any time from the privacy card.</p>"
        '<div class="gh-actions"><a class="gh-primary" href="https://teams.microsoft.com/">'
        f"{platform_mark('teams')}Back to Teams</a></div>"
    )
    return render_page(title="Connected Microsoft 365", context="Microsoft", body_html=body)


def build_oauth_microsoft_routes(
    *,
    graph: MicrosoftGraph,
    app_root_url: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[Handler, Handler]:
    """Return `(start, callback)` for this deployment's Entra app."""
    redirect_uri = f"{app_root_url.rstrip('/')}/oauth/microsoft/callback"

    async def start(request: Request) -> Response:
        state = request.query_params.get("state", "")
        async with graph.session_factory() as session:
            row = (
                await sign_ins.get_live_sign_in(session, state=state, now=now()) if state else None
            )
        if row is None:
            return _error_page("expired")
        challenge = generate_pkce(verifier=row.code_verifier).code_challenge
        return RedirectResponse(
            build_authorize_url(
                graph.config, redirect_uri=redirect_uri, state=state, code_challenge=challenge
            ),
            status_code=302,
        )

    async def callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        if not state:
            return _error_page("expired")
        async with graph.session_factory.begin() as session:
            row = await sign_ins.consume_sign_in(session, state=state, now=now())
        if row is None:
            return _error_page("expired")
        if not code:
            # Only the code is logged: Entra's description can echo request details.
            error = request.query_params.get("error", "")
            if error and error != "access_denied":
                log.warning("microsoft_connect.refused_by_entra", code=error[:64])
                return _error_page("blocked")
            return _error_page("declined")
        try:
            async with graph.http() as http:
                tokens = await exchange_code(
                    http,
                    graph.config,
                    code=code,
                    code_verifier=row.code_verifier,
                    redirect_uri=redirect_uri,
                )
            verify_identity(tokens, graph.config, expected_user_id=row.user_id)
        except MicrosoftIdentityMismatch as exc:
            log.warning("microsoft_connect.refused", reason=str(exc))
            async with graph.session_factory.begin() as session:
                await append_event(
                    session,
                    tenant_id=row.tenant_id,
                    account_id=row.account_id,
                    agent_id=None,
                    platform="teams",
                    platform_user_id=row.user_id,
                    tool_name="connect_microsoft",
                    operation="microsoft_connect",
                    outcome="denied",
                    reason=str(exc),
                )
            return _error_page("wrong_account")
        except MicrosoftAppRejected as exc:
            log.error("microsoft_accounts.app_rejected", code=str(exc))
            return _error_page("misconfigured")
        except (MicrosoftIdentityError, httpx.HTTPError) as exc:
            log.warning(
                "microsoft_connect.exchange_failed",
                err_type=type(exc).__name__,
                code=str(exc) if isinstance(exc, MicrosoftIdentityError) else None,
            )
            return _error_page("exchange_failed")
        if tokens.refresh_token is None:
            log.warning("microsoft_connect.no_refresh_token")
            return _error_page("exchange_failed")
        async with graph.session_factory.begin() as session:
            await tokens_store.upsert_microsoft_user_token(
                session,
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                encrypted_refresh_token=encrypt_token(graph.fernet, tokens.refresh_token),
                scopes=tokens.scope,
                now=now(),
            )
            await append_event(
                session,
                tenant_id=row.tenant_id,
                account_id=row.account_id,
                agent_id=None,
                platform="teams",
                platform_user_id=row.user_id,
                tool_name="connect_microsoft",
                operation="microsoft_connect",
                outcome="allowed",
                reason="connected",
            )
        return _success_page()

    return start, callback
