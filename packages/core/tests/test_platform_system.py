"""A Teams session runs a system prompt that names no other chat platform.

The shipped prompt covers every platform, and Discord and Slack sessions run it
unchanged: their `system_sha256` must never move, or every live session there is
replaced. Only Teams sessions carry a variant, sent as the session's own override.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.platform_system import (
    _TEAMS_PASSAGES,  # pyright: ignore[reportPrivateUsage]
    system_for_platform,
)
from daimon.core.session_snapshot import hash_system

_DEFAULTS = Path(__file__).resolve().parents[3] / "defaults"
#: The shipped prompt's hash before the Teams variant existed. A Discord or Slack
#: session recorded it, so a change here replaces every one of those sessions.
_SHIPPED_SYSTEM_SHA256 = "d79f72e4cd6d1128aeadce0fae9c4958b565a17ef282c2604272b1c3092283c3"
_OTHER_APPS = re.compile(r"discord|slack", re.IGNORECASE)


def _shipped_system() -> str:
    spec = yaml.safe_load((_DEFAULTS / "agents" / "daimon.yaml").read_text())
    return apply_credential_guidance(spec["system"])


def test_the_shipped_prompt_is_unchanged_for_discord_and_slack() -> None:
    system = _shipped_system()

    assert hash_system(system) == _SHIPPED_SYSTEM_SHA256
    for platform in ("discord", "slack", "cli", None):
        assert system_for_platform(system, platform) is None, platform


def test_every_teams_passage_is_in_the_shipped_prompt() -> None:
    system = _shipped_system()

    for shipped, _ in _TEAMS_PASSAGES:
        assert shipped in system, f"the shipped prompt no longer carries: {shipped[:60]!r}"


def test_the_teams_prompt_names_no_other_chat_platform() -> None:
    variant = system_for_platform(_shipped_system(), "teams")

    assert variant is not None
    assert _OTHER_APPS.search(variant) is None, _OTHER_APPS.search(variant)
    assert "Teams" in variant, "the Teams-only guidance stays"
    assert hash_system(variant) != _SHIPPED_SYSTEM_SHA256


def test_an_agent_without_the_shipped_passages_keeps_its_own_prompt() -> None:
    assert system_for_platform("You are a custom agent.", "teams") is None
    assert system_for_platform(None, "teams") is None
