"""connect_microsoft: a private, single-use sign-in card in the person's 1:1 chat."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.microsoft._person import require_person
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.adapters.mcp.tools.teams._direct import send_card_to_caller
from daimon.core.stores import microsoft_sign_ins as sign_ins
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

SIGN_IN_TTL = timedelta(minutes=10)


class ConnectMicrosoftResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["sent", "already_sent", "delivery_failed"]
    message: str


def connect_card(url: str) -> dict[str, object]:
    """The sign-in card; the link appears only in the button."""
    return {
        "type": "AdaptiveCard",
        "version": "1.5",
        "fallbackText": "Connect Microsoft 365",
        "body": [
            {
                "type": "TextBlock",
                "text": "Connect Microsoft 365",
                "size": "Large",
                "weight": "Bolder",
            },
            {
                "type": "TextBlock",
                "wrap": True,
                "text": (
                    "Sign in with your work account so Daimon can work with your files, "
                    "mail, calendar and notes as you. Mail, calendar and notes are shown "
                    "only in this chat."
                ),
            },
            {
                "type": "TextBlock",
                "wrap": True,
                "isSubtle": True,
                "text": "The link is yours alone, works once and expires in ten minutes.",
            },
        ],
        "actions": [{"type": "Action.OpenUrl", "title": "🔌 Connect Microsoft", "url": url}],
    }


async def connect_microsoft_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, origin_context_id: str
) -> ConnectMicrosoftResult:
    _graph, user_id = require_person(runtime, auth)
    root = runtime.settings.mcp.app_root_url
    if root is None:
        raise ToolError("Microsoft 365 access is not set up on this server.")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    now = datetime.now(UTC)
    async with runtime.session_factory.begin() as session:
        # One card per link lifetime: a repeated call must not keep messaging them.
        live = await sign_ins.get_live_sign_in_for_user(
            session, tenant_id=auth.tenant_id, user_id=user_id, now=now
        )
        if live is not None:
            return ConnectMicrosoftResult(
                status="already_sent",
                message=(
                    "A Connect Microsoft card is already in the person's 1:1 chat with "
                    f"Daimon and works until {live.expires_at:%H:%M} UTC, so no new card was "
                    "sent. Tell them to use it."
                ),
            )
        state = secrets.token_urlsafe(32)
        await sign_ins.create_sign_in(
            session,
            state=state,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            user_id=user_id,
            code_verifier=secrets.token_urlsafe(48),
            expires_at=now + SIGN_IN_TTL,
            now=now,
        )
    url = f"{str(root).rstrip('/')}/oauth/microsoft/start?state={state}"
    try:
        await send_card_to_caller(
            runtime, auth, conversation_id=origin.parent_channel_id, card=connect_card(url)
        )
    except Exception as exc:
        # Withdraw the link on any failure, so no half-sent sign-in is left behind.
        async with runtime.session_factory.begin() as session:
            await sign_ins.delete_sign_in(session, state=state)
        if not isinstance(exc, httpx.HTTPError | ValueError):
            raise
        return ConnectMicrosoftResult(
            status="delivery_failed",
            message=(
                "The sign-in card could not reach the person's 1:1 chat with Daimon (it may "
                "not be added for them personally), so nothing was sent or saved. Ask them to "
                "open a 1:1 chat with Daimon in Teams and ask there."
            ),
        )
    return ConnectMicrosoftResult(
        status="sent",
        message=(
            "The Connect Microsoft card is in the person's 1:1 chat with Daimon. Nothing is "
            "connected until they sign in. Say so in one line; never repeat the link."
        ),
    )
