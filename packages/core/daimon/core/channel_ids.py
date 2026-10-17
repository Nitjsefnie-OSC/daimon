"""Validate native platform channel identifiers before a scoped write."""

from __future__ import annotations

import re

_PATTERNS = {
    "discord": r"[0-9]{15,21}",
    "slack": r"[CG][A-Z0-9]+",
    "teams": r"19:[\w-]+@thread\.(?:tacv2|skype)",
}


class InvalidChannelId(ValueError):
    """A channel identifier does not match its platform's format."""


def normalize_channel_id(platform: str, channel_id: str) -> str:
    value = channel_id.strip()
    if platform == "slack" and ":" in value:
        value = value.split(":", 1)[0]
    elif platform == "teams" and ";messageid=" in value:
        value = value.split(";messageid=", 1)[0]
    pattern = _PATTERNS.get(platform)
    if pattern is None or re.fullmatch(pattern, value) is None:
        raise InvalidChannelId(f"invalid {platform} channel id {channel_id!r}")
    return value
