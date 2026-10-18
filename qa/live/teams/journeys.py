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
from qa.live.teams.graph import GraphTeamsDriver, PostOutcomeUnknown, graph_time


@dataclass(frozen=True)
class CommandJourney:
    role: Literal["admin", "member"]
    command: str
    expected: Literal["allow", "refuse"]
    card_text: str
    required_action: str | None = None
    absent_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedJourney:
    persona: str
    spec: CommandJourney


BUILTIN_JOURNEYS = {
    "routines-admin": CommandJourney("admin", "routines", "allow", "Routines", "New routine"),
    "routines-member": CommandJourney(
        "member", "routines", "refuse", "Routines", None, ("New routine",)
    ),
    "billing-member": CommandJourney(
        "member",
        "billing",
        "refuse",
        "Ask an admin to add credit.",
        None,
        ("Add credit", "Look up", "Look up a person"),
    ),
}

POINTERS = ("I've answered `routines` in our 1:1 chat.", "Commands work in our 1:1 chat.")


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


def is_new(message: dict[str, Any], seen_ids: frozenset[str], since: datetime) -> bool:
    """Exclude snapshot messages, including those inside the clock-skew window."""
    created = graph_time(message.get("createdDateTime"))
    return (
        bool(message.get("id"))
        and str(message["id"]) not in seen_ids
        and created is not None
        and created >= since - timedelta(seconds=10)
    )


async def run_journey(
    driver: GraphTeamsDriver, journey: ResolvedJourney, *, timeout: float = 120
) -> Result:
    """Snapshot, post once, and collect new channel and private-card evidence."""
    started = timestamp()
    correlation = str(uuid.uuid4())
    posted_id: str | None = None
    action_titles: list[str] = []
    spec = journey.spec
    try:
        channel_ids = await driver.channel_message_ids(journey.persona)
        old_cards = await driver.chat_messages_with_bot(
            journey.persona, datetime(1970, 1, 1, tzinfo=UTC)
        )
        chat_ids = frozenset(str(item["id"]) for item in old_cards if item.get("id"))
        since = datetime.now(UTC)
        message = await driver.post_mention(
            journey.persona, spec.command, correlation, command=True, seen_ids=channel_ids
        )
        posted_id = str(message["id"])
        deadline = driver.now() + timeout
        pointer = None
        card = None
        delay = 1.0
        while driver.now() < deadline and (pointer is None or card is None):
            if pointer is None:
                replies = await driver.read_replies(journey.persona, posted_id, deadline)
                pointer = next(
                    (
                        item
                        for item in replies
                        if is_new(item, channel_ids, since)
                        and any(
                            phrase in str(item.get("body", {}).get("content", ""))
                            for phrase in POINTERS
                        )
                    ),
                    None,
                )
            if card is None:
                messages = await driver.chat_messages_with_bot(journey.persona, since, deadline)
                card = next(
                    (
                        item
                        for item in messages
                        if is_new(item, chat_ids, since)
                        and any(spec.card_text in text for text in card_details(item)[0])
                    ),
                    None,
                )
            if pointer is None or card is None:
                await driver.sleep(min(delay, max(0, deadline - driver.now())))
                delay = min(delay * 2, 8)
        if pointer is None or card is None:
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
            any(spec.card_text in text for text in texts)
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
            [str(pointer["id"]), str(card["id"])],
            card_excerpt,
            classification,
            action_titles=action_titles,
            error_reason=(
                "CardValidationError: card did not match expected actions" if not valid else None
            ),
        )
    except Exception as exc:
        reason = "journey operation failed"
        if isinstance(exc, PostOutcomeUnknown):
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
            error_reason=f"{type(exc).__name__}: {reason}",
        )
