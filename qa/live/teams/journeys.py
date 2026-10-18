"""Data-driven persona journeys and Adaptive Card assertions."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import JsonValue

from qa.live.teams.config import Journey, TeamsQaConfig
from qa.live.teams.evidence import Result, excerpt, timestamp
from qa.live.teams.graph import GraphTeamsDriver


@dataclass(frozen=True)
class CommandJourney:
    role: Literal["admin", "member"]
    command: str
    expected: Literal["allow", "refuse"]
    card_text: str
    required_action: str | None = None
    absent_actions: tuple[str, ...] = ()


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


def resolve_journey(config: TeamsQaConfig, name: str) -> Journey:
    """Find a configured journey or bind a built-in one to its sole role persona."""
    if name in config.journeys:
        return config.journeys[name]
    spec = BUILTIN_JOURNEYS[name]
    personas = [key for key, identity in config.personas.items() if identity.role == spec.role]
    if len(personas) != 1:
        raise ValueError("journey role needs exactly one configured persona")
    return Journey(
        persona=personas[0], command=spec.command, expected=spec.expected, reply_in="chat"
    )


def card_details(message: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Collect text and action titles from Adaptive Card attachments."""
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


async def run_journey(
    driver: GraphTeamsDriver, journey: Journey, *, name: str = "", timeout: float = 120
) -> Result:
    """Post once and collect channel and private-card evidence before one deadline."""
    started = timestamp()
    since = datetime.now(UTC)
    correlation = str(uuid.uuid4())
    posted_id: str | None = None
    action_titles: list[str] = []
    spec = BUILTIN_JOURNEYS.get(name)
    try:
        message = await driver.post_mention(
            journey.persona, journey.command, correlation, command=spec is not None
        )
        posted_id = str(message["id"])
        deadline = driver.now() + timeout
        if spec is not None:
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
                            if any(
                                phrase in excerpt(str(item.get("body", {}).get("content", "")))
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
                            if any(spec.card_text in text for text in card_details(item)[0])
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
                [str(pointer.get("id", "")), str(card.get("id", ""))],
                card_excerpt,
                classification,
                action_titles=action_titles,
            )
        reply = None
        if journey.reply_in == "channel":
            reply = await driver.wait_for_reply(
                journey.persona, posted_id, lambda _r: True, deadline
            )
        else:
            while driver.now() < deadline:
                messages = await driver.chat_messages_with_bot(journey.persona, since, deadline)
                if messages:
                    reply = messages[-1]
                    break
                await driver.sleep(min(2, max(0, deadline - driver.now())))
    except Exception:
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
        )
    if reply is None:
        return Result(
            "live", journey.persona, correlation, posted_id, started, timestamp(), [], "", "timeout"
        )
    body = excerpt(str(reply.get("body", {}).get("content", "")))
    denied = any(phrase.casefold() in body.casefold() for phrase in journey.refusal_phrases)
    classification = "refused" if denied else "allowed"
    return Result(
        "live",
        journey.persona,
        correlation,
        posted_id,
        started,
        timestamp(),
        [str(reply.get("id", ""))],
        body,
        classification,
    )
