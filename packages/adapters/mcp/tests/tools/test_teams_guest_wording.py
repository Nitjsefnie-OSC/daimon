"""A person from another organisation is told no 1:1 chat is open to them.

Teams opens no 1:1 chat across organisations, so the tool results that point to one say
who the action is for instead. A local sender keeps the exact text it had.
"""

from __future__ import annotations

import typing
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._words import setup_instruction
from daimon.adapters.mcp.tools.skill_uploads import (  # pyright: ignore[reportPrivateUsage]
    NoCardReason,
    _no_card_preview,
    _no_card_refusal,
)
from daimon.adapters.mcp.tools.teams._files import post_files
from daimon.adapters.mcp.tools.teams._send import (  # pyright: ignore[reportPrivateUsage]
    _post_teams_wizard_impl,
)
from daimon.core.stores.domain import Role
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from fastmcp.exceptions import ToolError

_HOST = "organisation that added Daimon"
_GROUP_CHAT = "19:group@thread.v2"
_SPEC = WizardSpec(
    prompt="Pick",
    steps=[
        Step(
            key="stage",
            question="Pick a stage",
            kind=StepKind.CHOICE,
            options=[Option(label="dev", value="dev")],
        )
    ],
)
_REASONS = typing.get_args(NoCardReason)


def _auth() -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="teams",
        external_id="entra",
        platform_user_id="aad",
    )


def test_setup_is_asked_of_the_host_organisation_for_a_guest() -> None:
    guest = setup_instruction("teams", foreign=True)

    assert guest == f"ask someone in the {_HOST} to send `setup`"
    assert setup_instruction("teams") == "send `setup` in a 1:1 chat with Daimon"


@pytest.mark.parametrize("reason", _REASONS)
def test_the_add_skill_fallbacks_send_a_guest_to_no_one_to_one_chat(reason: NoCardReason) -> None:
    for foreign in (False, True):
        refusal = _no_card_refusal("teams", reason, "helper", foreign=foreign)
        preview = _no_card_preview("teams", reason, "helper", foreign=foreign)
        for text in (refusal, preview):
            assert ("1:1 chat" in text) is not foreign, text
            assert (_HOST in text) is foreign, text


@pytest.mark.parametrize("foreign", [False, True])
async def test_a_form_refused_in_a_group_chat_names_a_one_to_one_chat_only_to_a_local_sender(
    foreign: bool,
) -> None:
    with (
        patch(
            "daimon.adapters.mcp.tools.teams._send.caller_is_foreign",
            new=AsyncMock(return_value=foreign),
        ),
        pytest.raises(ToolError) as refused,
    ):
        await _post_teams_wizard_impl(
            MagicMock(),
            _auth(),
            channel_id=_GROUP_CHAT,
            spec=_SPEC,
            short_id="s",
        )

    assert ("1:1" in str(refused.value)) is not foreign


@pytest.mark.parametrize("foreign", [False, True])
async def test_files_refused_in_a_group_chat_name_a_one_to_one_chat_only_to_a_local_sender(
    foreign: bool,
) -> None:
    with (
        patch(
            "daimon.adapters.mcp.tools.teams._files.caller_is_foreign",
            new=AsyncMock(return_value=foreign),
        ),
        pytest.raises(ToolError) as refused,
    ):
        await post_files(MagicMock(), _auth(), MagicMock(), _GROUP_CHAT, "hi", [])

    assert ("1:1" in str(refused.value)) is not foreign
