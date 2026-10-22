"""An admin's chat request to run one channel on another model provider.

``request_channel_backend_switch`` previews, then applies after the requesting
admin confirms on the card, through `daimon.core.channel_backend_switch`.
Registered only where backend selection is on (mux turns with channel
backends), like the agent switch.
"""

from __future__ import annotations

from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import resolve_channel
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import get_chat_origin
from daimon.core.channel_backend import backend_selection_enabled
from daimon.core.channel_backend_switch import (
    ChannelSwitchRefused,
    apply_channel_backend_switch,
    preview_channel_backend_switch,
)
from daimon.core.errors import DaimonError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.ids import Provider
from pydantic import BaseModel

_FROM_CHAT = "Request this change from an admin's active chat conversation."


class ChannelBackendSwitchInfo(BaseModel):
    status: Literal["preview", "changed"]
    channel_id: str
    target_backend: Provider
    model: str | None
    models: tuple[str, ...]
    current_backend: Provider
    current_model: str | None
    expected_revision: int
    preview_digest: str
    not_carried: tuple[str, ...]
    carries_over: str
    cost_line: str
    summary: str


async def _request_channel_backend_switch_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    target_backend: Provider,
    model: str | None = None,
    channel_id: str | None = None,
    expected_revision: int | None = None,
    preview_digest: str | None = None,
    origin_context_id: str | None = None,
) -> ChannelBackendSwitchInfo:
    _require_admin(auth)
    if auth.agent_id is not None or auth.platform_user_id is None:
        raise ToolError(_FROM_CHAT)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    if origin is None:
        raise ToolError(_FROM_CHAT)
    if not backend_selection_enabled(runtime.settings.turn):
        raise ToolError("Choosing a model provider is turned off. Nothing changed.")

    from daimon.adapters.mcp.tools._session_gate import session_card_gap

    if (
        await session_card_gap(runtime, auth, origin, tool_name="request_channel_backend_switch")
        is not None
    ):
        raise ToolError("This conversation cannot verify the confirmation card. Nothing changed.")
    if channel_id is None and preview_digest is not None:
        raise ToolError("Pass the preview's channel_id with its preview_digest. Nothing changed.")
    target = origin.parent_channel_id
    if channel_id is not None and channel_id != target:
        target = (await resolve_channel(runtime, auth, channel_id)).channel_id
    try:
        preview = await preview_channel_backend_switch(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            channel_id=target,
            target_backend=target_backend,
            model=model,
        )
        if not preview.allowed:
            raise ToolError(f"{preview.refusal} Nothing changed.")
        status: Literal["preview", "changed"] = "preview"
        summary = (
            "Show this preview, including what doesn't carry over and the cost line. If they "
            "want it, call request_channel_backend_switch again with the same target_backend "
            "and model plus this channel_id, expected_revision and preview_digest; the "
            "requesting admin must Confirm on the card. Nothing has changed yet."
        )
        if preview_digest is not None:
            if expected_revision is None:
                raise ToolError(
                    "Pass the preview's expected_revision with its preview_digest. Nothing changed."
                )
            preview, _ = await apply_channel_backend_switch(
                runtime.session_factory,
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                subject=mcp_subject(auth, is_admin=auth.is_admin),
                platform=origin.platform,
                channel_id=target,
                target_backend=target_backend,
                model=model,
                expected_revision=expected_revision,
                expected_preview_digest=preview_digest,
            )
            status = "changed"
            summary = (
                f"Switched to {preview.target_label}. The next new conversation in this "
                "channel uses it; existing threads keep their provider. "
                + preview.carries_over_line
            )
    except ChannelSwitchRefused as exc:
        raise ToolError(f"{exc} Nothing changed.") from exc
    except DaimonError as exc:
        raise ToolError(
            "The preview is unavailable or changed. Preview again; nothing changed."
        ) from exc
    return ChannelBackendSwitchInfo(
        status=status,
        channel_id=preview.channel_id,
        target_backend=preview.target_backend,
        model=preview.model,
        models=preview.models,
        current_backend=preview.current_backend,
        current_model=preview.current_model,
        expected_revision=preview.expected_revision,
        preview_digest=preview.digest,
        not_carried=preview.not_carried,
        carries_over=preview.carries_over_line,
        cost_line=preview.cost_line,
        summary=summary,
    )


def register_channel_backend_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    if not backend_selection_enabled(runtime.settings.turn):
        return

    @mcp.tool(tags={"admin"})
    async def request_channel_backend_switch(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        target_backend: Provider,
        model: str | None = None,
        channel_id: str | None = None,
        expected_revision: int | None = None,
        preview_digest: str | None = None,
        origin_context_id: str | None = None,
    ) -> ChannelBackendSwitchInfo:
        """Run this channel on Gemini or OpenAI, or switch it back to the default (anthropic).

        Use when an admin asks to fork or move the agent here to another provider or
        model: "fork agent to gemini", "switch this channel to gemini-4-argon",
        "use OpenAI here", "switch back to Claude". No new agent is needed. ``model``
        picks one of the returned ``models`` (Gemini: gemini-3.8-flash,
        gemini-3.5-flash-lite or gemini-4-argon); omit it for the provider's default.
        ``channel_id`` defaults to this turn's channel; name another channel to switch
        it from here.
        First call without preview_digest and show the preview, including what
        doesn't carry over (the channel answers in text only) and the cost line.
        Call again with the same arguments plus the preview's channel_id,
        expected_revision and preview_digest: the requesting admin must Confirm on
        the card before anything changes. Pass this active turn's origin_context_id.
        """
        return await _request_channel_backend_switch_impl(
            runtime,
            await _auth(ctx),
            target_backend=target_backend,
            model=model,
            channel_id=channel_id,
            expected_revision=expected_revision,
            preview_digest=preview_digest,
            origin_context_id=origin_context_id,
        )
