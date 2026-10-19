"""`answer_message`: the text format each kind of answer chunk goes out in."""

from __future__ import annotations

from typing import Any

from daimon.adapters.teams import card
from daimon.core.turn.state import TurnState
from microsoft_teams.api import Account, MentionEntity


def test_answer_chunk_is_extended_markdown_with_currency_escaped() -> None:
    """A plain chunk renders math and tables; a price's `$` stays literal."""
    message = card.answer_message("Costs $5. Area: $\\pi r^2$.")
    assert message.text_format == "extendedmarkdown", "answers use Extended Markdown"
    assert message.text == "Costs \\$5. Area: $\\pi r^2$.", "currency escaped, math kept"


def test_answer_chunk_with_a_mention_stays_markdown() -> None:
    """A completion ping keeps plain markdown: Extended Markdown's `<at>` support is unknown."""
    requester = Account(id="00000000-0000-0000-0000-000000000001", name="Ada")
    message = card.answer_message("Costs $5.", mention=requester)
    assert message.text_format == "markdown", "a mention keeps the format known to ping"
    assert message.text == "<at>Ada</at>\n\nCosts $5.", "markdown needs no dollar escape"
    assert any(isinstance(e, MentionEntity) for e in message.entities or []), "mention entity"


def _live(state: card.CardState) -> dict[str, Any]:
    activity = card.status_card(state, now=165.0, cancel_key="k")
    return activity.model_dump(by_alias=True)["attachments"][0]["content"]


def test_the_stopping_card_says_so_has_no_stop_button_and_keeps_the_elapsed_time() -> None:
    working = _live(card.CardState(started_at=100.0))
    stopping = _live(card.on_stop(card.CardState(started_at=100.0)))
    assert "ActionSet" in str(working) and card.CANCEL_VERB in str(working)
    headline, elapsed = stopping["body"]
    assert headline["text"] == "**Stopping…**" and elapsed["text"] == "1m 5s"
    assert "ActionSet" not in str(stopping) and card.CANCEL_VERB not in str(stopping)
    assert stopping["fallbackText"] == card.STOPPING_HEADLINE


def test_stopping_survives_the_turns_later_activity() -> None:
    state = card.on_activity(card.on_stop(card.CardState(started_at=0.0)), TurnState())
    assert state.stopping
