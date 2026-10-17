"""Plain wording and spacing in the Teams channel settings card."""

import pytest
from daimon.adapters.teams.channel_settings import (
    CHANNELS_NEED_ADMIN,
    channels_need_admin_message,
)
from daimon.adapters.teams.channel_settings_card import ChannelSettings, channel_settings_form
from daimon.core.config import reset_channel_admins_enabled
from microsoft_teams.cards import TextBlock


def test_channel_admin_explainer_and_id_hint_are_spaced_blocks() -> None:
    card = channel_settings_form(
        ChannelSettings(
            channel_id="analytics",
            label="#analytics",
            picker=None,
            rule=None,
            admin_user_ids=(),
            skills=(),
        )
    )
    blocks = [item for item in card.body if isinstance(item, TextBlock)]
    texts = [item.text for item in blocks]
    assert (
        "Channel admins choose their channels' environment and agent, and edit agents "
        "limited to those channels.\n\n"
        "Starting agents and the server default stay with server admins."
    ) in texts
    assert (
        "Enter Entra object IDs, separated by commas.\n\nLeave it empty to remove them."
    ) in texts
    assert (
        "These skills apply only here, from the next message.\n\nOnly server admins can change them."
        in texts
    )
    hint = next(item for item in blocks if item.text.startswith("Enter Entra object IDs"))
    assert hint.spacing == "Medium"


def test_channel_admin_fields_are_hidden_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_channel_admins_enabled()
    monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "false")
    try:
        card = channel_settings_form(
            ChannelSettings(
                channel_id="analytics",
                label="#analytics",
                picker=None,
                rule=None,
                admin_user_ids=("user",),
                skills=(),
            )
        )
        assert "Channel admins" not in str(card.model_dump())
    finally:
        reset_channel_admins_enabled()


def test_disabled_refusal_names_only_server_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_channel_admins_enabled()
    monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "false")
    try:
        assert channels_need_admin_message() == "Only a server admin can change this."
        reset_channel_admins_enabled()
        monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "true")
        assert channels_need_admin_message() == CHANNELS_NEED_ADMIN
    finally:
        reset_channel_admins_enabled()
