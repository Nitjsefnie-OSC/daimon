"""Built-in Teams command journeys and Adaptive Card assertions."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import JsonValue

from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.evidence import Result, excerpt, timestamp
from qa.live.teams.graph import (
    GraphTeamsDriver,
    NoBotChat,
    PersonaCollision,
    PostOutcomeUnknown,
    bot_sender_id,
    graph_time,
    user_sender_id,
)


@dataclass(frozen=True)
class CommandJourney:
    role: Literal["admin", "member"]
    command: str
    expected: Literal["allow", "refuse"]
    card_text: str
    required_action: str | None = None
    absent_actions: tuple[str, ...] = ()
    exact_card_text: bool = False


@dataclass(frozen=True)
class ResolvedJourney:
    persona: str
    spec: CommandJourney


BUILTIN_JOURNEYS = {
    "routines-admin": CommandJourney("admin", "routines", "allow", "Routines", "New routine"),
    "routines-member": CommandJourney(
        "member", "routines", "refuse", "Routines", "Refresh", ("New routine",), True
    ),
    "billing-admin": CommandJourney("admin", "billing", "allow", "", "Add credit"),
    "billing-member": CommandJourney(
        "member",
        "billing",
        "refuse",
        "Ask an admin to add credit.",
        None,
        ("Add credit", "Look up", "Look up a person"),
    ),
}


def resolve_journey(config: TeamsQaConfig, name: str) -> ResolvedJourney:
    """Bind a built-in command to the sole configured persona with its role."""
    try:
        spec = BUILTIN_JOURNEYS[name]
    except KeyError:
        raise ValueError("unknown built-in journey") from None
    personas = [key for key, identity in config.personas.items() if identity.role == spec.role]
    if len(personas) != 1:
        raise ValueError("journey role needs exactly one configured persona")
    return ResolvedJourney(personas[0], spec)


def card_details(message: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Collect full text and action titles from Adaptive Card attachments."""
    texts: set[str] = set()
    titles: set[str] = set()

    def walk(value: JsonValue) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            card_type = value.get("type")
            card_text = value.get("text")
            title = value.get("title")
            if card_type == "TextBlock" and isinstance(card_text, str):
                texts.add(card_text)
            if str(card_type).startswith("Action.") and isinstance(title, str):
                titles.add(title)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    walk(nested)

    for attachment in message.get("attachments", []):
        content = attachment.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                continue
        walk(cast(JsonValue, content))
    return texts, titles


def is_new(message: dict[str, Any], seen_ids: frozenset[str], posted_at: datetime) -> bool:
    """Require an unseen message no earlier than the server's post time."""
    created = graph_time(message.get("createdDateTime"))
    return (
        bool(message.get("id"))
        and str(message["id"]) not in seen_ids
        and created is not None
        and created >= posted_at
    )


class ChatNotQuiet(ValueError):
    """A recent persona command has no later bot message in this chat."""


def chat_is_quiet(
    messages: list[dict[str, Any]], persona_oid: str, bot_app_id: str, now: datetime
) -> bool:
    """Require a bot response after every persona post in the last 30 seconds."""
    bot_times = [
        created
        for item in messages
        if bot_sender_id(item) == bot_app_id
        if (created := graph_time(item.get("createdDateTime"))) is not None
    ]
    newest_bot = max(bot_times, default=None)
    threshold = now - timedelta(seconds=30)
    for item in messages:
        if user_sender_id(item) != persona_oid:
            continue
        created = graph_time(item.get("createdDateTime"))
        if created is None or (
            created >= threshold and (newest_bot is None or newest_bot <= created)
        ):
            return False
    return True


async def run_journey(
    driver: GraphTeamsDriver, journey: ResolvedJourney, *, timeout: float = 120
) -> Result:
    """Post once in the existing bot chat and wait for a new bot card."""
    started = timestamp()
    correlation = str(uuid.uuid4())
    posted_id: str | None = None
    action_titles: list[str] = []
    spec = journey.spec
    try:
        persona_oids = driver.require_distinct_personas()
        chat_id, old_messages = await driver.bot_chat(journey.persona)
        if not chat_is_quiet(
            old_messages, persona_oids[journey.persona], driver.config.bot_app_id, datetime.now(UTC)
        ):
            raise ChatNotQuiet("chat not quiet")
        chat_ids = frozenset(str(item["id"]) for item in old_messages if item.get("id"))
        message = await driver.post_chat_command(
            journey.persona, chat_id, spec.command, old_messages
        )
        posted_id = str(message["id"])
        posted_at = graph_time(message.get("createdDateTime"))
        if posted_at is None:
            raise ValueError("posted message timestamp missing")
        deadline = driver.now() + timeout
        card = None
        delay = 1.0
        while driver.now() < deadline and card is None:
            messages = await driver.chat_messages(journey.persona, chat_id, deadline)
            card = next(
                (
                    item
                    for item in messages
                    if is_new(item, chat_ids | {posted_id}, posted_at)
                    and bot_sender_id(item) == driver.config.bot_app_id
                    and bool(card_details(item)[0])
                ),
                None,
            )
            if card is None:
                await driver.sleep(min(delay, max(0, deadline - driver.now())))
                delay = min(delay * 2, 8)
        if card is None:
            return Result(
                "live",
                journey.persona,
                correlation,
                posted_id,
                started,
                timestamp(),
                [],
                "",
                "timeout",
            )
        texts, titles = card_details(card)
        action_titles = sorted(titles)
        card_excerpt = excerpt(str(card.get("body", {}).get("content", "")))
        if not card_excerpt:
            card_excerpt = excerpt(" ".join(sorted(texts)))
        valid = (
            (
                spec.card_text in texts
                if spec.exact_card_text
                else any(spec.card_text in text for text in texts)
            )
            and (spec.required_action is None or spec.required_action in titles)
            and not any(action in titles for action in spec.absent_actions)
        )
        classification = (
            ("allowed" if spec.expected == "allow" else "refused") if valid else "error"
        )
        return Result(
            "live",
            journey.persona,
            correlation,
            posted_id,
            started,
            timestamp(),
            [str(card["id"])],
            card_excerpt,
            classification,
            action_titles=action_titles,
            error_reason=(
                "CardValidationError: card did not match expected actions" if not valid else None
            ),
        )
    except Exception as exc:
        reason = "journey operation failed"
        if isinstance(exc, NoBotChat):
            reason = "no bot chat: sign in to Teams once as this persona"
        elif isinstance(exc, ChatNotQuiet):
            reason = "chat not quiet"
        elif isinstance(exc, PersonaCollision):
            reason = "admin and member token identities match"
        elif isinstance(exc, PostOutcomeUnknown):
            reason = "post outcome unknown"
        elif isinstance(exc, ValueError) and str(exc) == "bot chat ambiguous":
            reason = "bot chat ambiguous"
        return Result(
            "live",
            journey.persona,
            correlation,
            posted_id,
            started,
            timestamp(),
            [],
            "",
            "error",
            action_titles=action_titles,
            error_reason=(
                reason
                if isinstance(exc, (NoBotChat, ChatNotQuiet))
                else f"{type(exc).__name__}: {reason}"
            ),
        )
