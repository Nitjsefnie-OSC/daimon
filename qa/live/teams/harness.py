"""Small, JSON-safe primitives for agent-driven exploratory Teams QA."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any, cast

from qa.live.teams.graph import (
    AmbiguousPostError,
    GraphRequestError,
    GraphTeamsDriver,
    PostOutcomeUnknown,
    bot_sender_id,
    graph_time,
)
from qa.live.teams.journeys import ChatNotQuiet, card_details, chat_is_quiet
from qa.live.teams.tokens import ReauthRequired

MAX_MESSAGES = 50
READ_DEADLINE = 30.0
QUIET_SECONDS = 3.0


def _error(exc: Exception, persona: str) -> dict[str, Any]:
    if isinstance(exc, ReauthRequired):
        return {"ok": False, "error": "reauth_required", "persona": persona}
    if isinstance(exc, (AmbiguousPostError, PostOutcomeUnknown)):
        return {"ok": False, "error": "post_outcome_unknown", "persona": persona}
    if isinstance(exc, ChatNotQuiet):
        return {"ok": False, "error": "chat_not_quiet", "persona": persona}
    if isinstance(exc, ValueError):
        return {"ok": False, "error": str(exc), "persona": persona}
    if isinstance(exc, GraphRequestError):
        return {"ok": False, "error": "graph_request_failed", "persona": persona}
    raise exc


def _persona(driver: GraphTeamsDriver, persona: str) -> None:
    if persona not in driver.config.personas:
        raise ValueError("unknown_persona")


def _source(source: str) -> None:
    if source in ("chat", "channel"):
        return
    if source.startswith("thread:") and source[7:] and "/" not in source[7:]:
        return
    raise ValueError("invalid_source")


def parse_since(since: str) -> timedelta:
    """Parse seconds, minutes, or hours with a 24-hour maximum."""
    match = re.fullmatch(r"([1-9][0-9]*)([smh])", since)
    if match is None:
        raise ValueError("invalid_since")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]
    if seconds > 86400:
        raise ValueError("since_exceeds_24h")
    return timedelta(seconds=seconds)


def _plain(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return unescape(re.sub(r"<[^>]*>", "", value)).strip()


def _format(message: dict[str, Any]) -> dict[str, Any]:
    texts, titles = card_details(message)
    body = message.get("body")
    content: object = (
        cast(dict[str, object], body).get("content") if isinstance(body, dict) else None
    )
    return {
        "id": str(message.get("id", "")),
        "created": str(message.get("createdDateTime", "")),
        "text": _plain(content),
        "card_texts": sorted(texts),
        "action_titles": sorted(titles),
    }


async def _messages(
    driver: GraphTeamsDriver, persona: str, source: str, deadline: float
) -> list[dict[str, Any]]:
    if source == "chat":
        chat_id, _ = await driver.bot_chat(persona)
        return await driver.chat_messages(persona, chat_id, deadline)
    if source == "channel":
        return await driver.channel_messages(persona, deadline)
    return await driver.read_replies(persona, source[7:], deadline)


def _bot_messages(
    messages: list[dict[str, Any]], bot_app_id: str, since: datetime | None = None
) -> list[dict[str, Any]]:
    filtered = [
        item
        for item in messages
        if bot_sender_id(item) == bot_app_id
        and (created := graph_time(item.get("createdDateTime"))) is not None
        and (since is None or created > since)
        and item.get("id")
    ]
    filtered.sort(key=lambda item: (graph_time(item["createdDateTime"]), str(item["id"])))
    return filtered[-MAX_MESSAGES:]


async def send(driver: GraphTeamsDriver, persona: str, to: str, text: str) -> dict[str, Any]:
    """Send exactly one Graph POST after identity, target, and chat checks."""
    try:
        _persona(driver, persona)
        if to not in ("chat", "channel"):
            raise ValueError("invalid_to")
        if not text.strip():
            raise ValueError("invalid_text")
        oids = driver.require_distinct_personas()
        await driver.ensure_target_verified(persona)
        if to == "chat":
            chat_id, snapshot = await driver.bot_chat(persona)
            if not chat_is_quiet(
                snapshot, oids[persona], driver.config.bot_app_id, datetime.now(UTC)
            ):
                raise ChatNotQuiet("chat not quiet")
            message = await driver.post_chat_command(persona, chat_id, text, snapshot)
            target = {"chat_id": chat_id}
        else:
            message = await driver.post_mention(persona, text, str(uuid.uuid4()))
            target = {"channel_id": driver.config.channel_id}
        message_id = message.get("id")
        posted_at = message.get("createdDateTime")
        if not isinstance(message_id, str) or not message_id or graph_time(posted_at) is None:
            raise GraphRequestError("Graph post receipt incomplete")
        return {
            "ok": True,
            "persona": persona,
            "to": to,
            "message_id": message_id,
            "posted_at": posted_at,
            **target,
        }
    except (ReauthRequired, GraphRequestError, ValueError) as exc:
        return _error(exc, persona)


async def read(driver: GraphTeamsDriver, persona: str, source: str, since: str) -> dict[str, Any]:
    """Return at most 50 recent bot messages, oldest first."""
    try:
        _persona(driver, persona)
        _source(source)
        duration = parse_since(since)
        messages = await _messages(driver, persona, source, driver.now() + READ_DEADLINE)
        cutoff = datetime.now(UTC) - duration
        return {
            "ok": True,
            "persona": persona,
            "source": source,
            "messages": [
                _format(item) for item in _bot_messages(messages, driver.config.bot_app_id, cutoff)
            ],
        }
    except (ReauthRequired, GraphRequestError, ValueError) as exc:
        return _error(exc, persona)


async def wait(
    driver: GraphTeamsDriver,
    persona: str,
    after: str,
    timeout: float,
    source: str = "chat",
) -> dict[str, Any]:
    """Poll for bot messages after a server-timed post and settle a quiet window."""
    try:
        _persona(driver, persona)
        _source(source)
        if not after or "/" in after:
            raise ValueError("invalid_after")
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError("invalid_timeout")
        deadline = driver.now() + timeout
        after_time: datetime | None = None
        quiet_since: float | None = None
        prior_ids: frozenset[str] = frozenset()
        while driver.now() < deadline:
            messages = await _messages(driver, persona, source, deadline)
            if after_time is None:
                anchor = next((item for item in messages if str(item.get("id")) == after), None)
                if anchor is None and source.startswith("thread:") and source[7:] == after:
                    anchor = await driver.channel_message(persona, after, deadline)
                if (
                    anchor is None
                    or (after_time := graph_time(anchor.get("createdDateTime"))) is None
                ):
                    raise ValueError("after_not_found")
            found = _bot_messages(messages, driver.config.bot_app_id, after_time)
            ids = frozenset(str(item["id"]) for item in found)
            if found:
                if ids != prior_ids:
                    prior_ids = ids
                    quiet_since = driver.now()
                elif quiet_since is not None and driver.now() - quiet_since >= QUIET_SECONDS:
                    return {
                        "ok": True,
                        "persona": persona,
                        "source": source,
                        "messages": [_format(item) for item in found],
                    }
            await driver.sleep(min(1.0, max(0.0, deadline - driver.now())))
        return {"ok": False, "error": "timeout", "after": after}
    except (ReauthRequired, GraphRequestError, ValueError) as exc:
        return _error(exc, persona)
