"""TeamsReactions: background, rate-spaced reactions that never fail their caller."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Coroutine
from typing import Any

import httpx
from daimon.adapters.teams.reactions import (
    CHECK,
    EYES,
    HOURGLASS,
    MAX_WAIT_S,
    MIN_INTERVAL_S,
    ReactionTarget,
    TeamsReactions,
    reaction_target,
)

from .conftest import make_inbound


class Calls:
    """A `React` recording each call; reactions in `fail` raise a 429 first."""

    def __init__(self, *, fail: frozenset[str] = frozenset()) -> None:
        self.made: list[tuple[str, str, bool]] = []
        self.fail = fail

    async def __call__(self, target: ReactionTarget, reaction: str, *, add: bool) -> None:
        self.made.append((target.activity_id, reaction, add))
        if reaction in self.fail:
            request = httpx.Request("PUT", "https://smba.example.test/v3/conversations/c")
            response = httpx.Response(429, headers={"Retry-After": "1"}, request=request)
            raise httpx.HTTPStatusError("throttled", request=request, response=response)


class Harness:
    """A frozen clock, recorded sleeps and the tasks the reactions spawned."""

    def __init__(self, react: Calls | None) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.reactions = TeamsReactions(
            react, spawn=self.spawn, clock=lambda: self.now, sleep=self.sleep
        )

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks)


async def test_a_turn_reacts_eyes_then_swaps_them_for_a_check_in_order() -> None:
    calls = Calls()
    harness = Harness(calls)
    inbound = make_inbound()
    acknowledge = harness.reactions.acknowledger(inbound)
    assert acknowledge is not None, "a person's message can be reacted to"
    acknowledge("accepted")
    acknowledge("done")
    await harness.settle()
    key = inbound.activity_id
    assert calls.made == [(key, EYES, True), (key, CHECK, True), (key, EYES, False)], (
        "👀 lands first, then ✅, and only then do the eyes go"
    )
    assert harness.slept == [MIN_INTERVAL_S, 2 * MIN_INTERVAL_S], (
        "calls are spaced to the bot's two-a-second limit"
    )


async def test_a_queued_message_shows_an_hourglass_until_its_work_starts() -> None:
    calls = Calls()
    harness = Harness(calls)
    first, second = make_inbound("one"), make_inbound("two")
    harness.reactions.mark_queued(first)
    harness.reactions.mark_queued(second)
    composed = dataclasses.replace(second, composed_ids=(first.activity_id,))
    harness.reactions.clear_queued([composed])
    harness.reactions.clear_queued([composed])
    await harness.settle()
    assert sorted(calls.made) == sorted(
        [
            (first.activity_id, HOURGLASS, True),
            (second.activity_id, HOURGLASS, True),
            (first.activity_id, HOURGLASS, False),
            (second.activity_id, HOURGLASS, False),
        ]
    ), "each queued message folded into the turn loses its ⌛ once, a second clear is a no-op"
    assert calls.made.index((first.activity_id, HOURGLASS, True)) < calls.made.index(
        (first.activity_id, HOURGLASS, False)
    ), "a removal never overtakes the add it undoes"


async def test_a_throttled_reaction_is_dropped_and_the_next_still_runs() -> None:
    calls = Calls(fail=frozenset({EYES}))
    harness = Harness(calls)
    target = reaction_target(make_inbound())
    assert target is not None, "a person's message can be reacted to"
    harness.reactions.acknowledge(target, "accepted")
    harness.reactions.acknowledge(target, "done")
    await harness.settle()
    assert [reaction for _, reaction, _ in calls.made] == [EYES, CHECK, EYES], (
        "a 429 is not retried, and the calls after it still run"
    )
    assert all(task.exception() is None for task in harness.tasks), "no failure escapes"


async def test_reactions_past_the_wait_budget_are_skipped_not_queued() -> None:
    calls = Calls()
    harness = Harness(calls)
    budget = int(MAX_WAIT_S / MIN_INTERVAL_S) + 1
    for _ in range(budget + 3):
        harness.reactions.mark_queued(make_inbound())
    await harness.settle()
    assert len(calls.made) == budget, "a burst beyond the bounded wait drops its extra reactions"


async def test_nothing_reacts_to_a_form_a_reply_nobody_addressed_or_without_a_client() -> None:
    form = dataclasses.replace(make_inbound(), from_form=True)
    unprompted = dataclasses.replace(make_inbound(), unprompted=True)
    assert reaction_target(form) is None, "a form's submit names no message"
    assert reaction_target(unprompted) is None, "nobody asked, so nothing is signalled"
    harness = Harness(None)
    assert harness.reactions.acknowledger(make_inbound()) is None, "no client, no reactions"
    harness.reactions.mark_queued(make_inbound())
    assert harness.tasks == [], "without a client nothing is spawned"
