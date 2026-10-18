"""microsoft_send waits for the person's Approve wherever Microsoft accounts are on."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from daimon.core.confirmation import confirmed_tools_for_turn, prompt_for_tool_call
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ToolCall,
    ToolSafetyPolicy,
    decide_tool_call,
    has_confirmation_gate,
    microsoft_send_tools,
    session_tools_for_policy,
)
from daimon.core.turn.deps import TurnDeps

_TRUSTED = frozenset({DAIMON_SERVER_NAME})
_TOOLS: list[dict[str, object]] = [
    {
        "type": "mcp_toolset",
        "mcp_server_name": DAIMON_SERVER_NAME,
        "default_config": {"permission_policy": {"type": "always_allow"}},
    }
]


def _call(**data: object) -> ToolCall:
    return ToolCall(
        tool_use_id="t1",
        server_name=DAIMON_SERVER_NAME,
        tool_name="microsoft_send",
        input={"action": "send_mail", "to": ["ada@example.com"], "subject": "Notes", **data},
    )


def test_turns_select_microsoft_send_only_when_configured() -> None:
    on = cast(TurnDeps, SimpleNamespace(backend_selection_enabled=False, microsoft_send=True))
    off = cast(TurnDeps, SimpleNamespace(backend_selection_enabled=False, microsoft_send=False))
    assert "microsoft_send" in confirmed_tools_for_turn(on), "configured: sessions must ask"
    assert not confirmed_tools_for_turn(off), "unconfigured: nothing extra is asked"


def test_selected_microsoft_send_asks_with_tool_safety_off() -> None:
    selected = microsoft_send_tools(enabled=True)
    asked = decide_tool_call(
        OPEN_TOOL_SAFETY, _call(), attended=True, trusted_servers=_TRUSTED, confirmed_tools=selected
    )
    assert asked.outcome == "ask", "an attended send must wait for the card"
    unattended = decide_tool_call(
        OPEN_TOOL_SAFETY,
        _call(),
        attended=False,
        trusted_servers=_TRUSTED,
        confirmed_tools=selected,
    )
    assert unattended.outcome == "deny", "a run nobody watches must never send as the person"
    gated = session_tools_for_policy(
        OPEN_TOOL_SAFETY, _TOOLS, trusted_servers=_TRUSTED, confirmed_tools=selected
    )
    assert gated is not None and has_confirmation_gate(gated, tool_name="microsoft_send"), (
        "the session must hold microsoft_send on always_ask, the gate the tool checks"
    )


def test_microsoft_send_is_not_asked_unless_selected() -> None:
    gated = session_tools_for_policy(
        ToolSafetyPolicy(enabled=True), _TOOLS, trusted_servers=_TRUSTED
    )
    assert gated is None or not has_confirmation_gate(gated, tool_name="microsoft_send"), (
        "a deployment without Microsoft accounts must not gain a config for an absent tool"
    )


def test_card_shows_every_recipient_location_and_body_without_details() -> None:
    """Who and what goes out is in the always-visible text, never truncated."""
    long_cc = "a.very.long.mailbox.name.for.an.optional.attendee@subsidiary.example.com"
    prompt = prompt_for_tool_call(
        _call(
            action="create_event",
            start="2026-09-15T10:00",
            end="2026-09-15T11:00",
            cc=["bob@example.com", long_cc],
            location="Room 4",
            body="Agenda:\n  budget review",
        ),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    shown = prompt.consequence or ""
    assert prompt.title == 'Send invite "Notes"?', "the card must say what goes out"
    assert shown.startswith("It goes out from your Microsoft account."), "sent as the person"
    assert "To: ada@example.com" in shown, "the card must list the required attendees"
    assert f"Optional: bob@example.com, {long_cc}" in shown, "every cc address, untruncated"
    assert "Where: Room 4" in shown, "an invite shows where"
    assert "Message: Agenda: budget review" in shown, "the card previews the body"
    assert any(line.startswith("When: ") for line in prompt.detail_lines), "an invite shows when"


def test_mail_card_labels_cc_recipients() -> None:
    """A mail's cc list is shown as Cc."""
    prompt = prompt_for_tool_call(
        _call(cc=["eve@example.com"], body="Hi"),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    assert "Cc: eve@example.com" in (prompt.consequence or ""), "mail cc must be visible"
