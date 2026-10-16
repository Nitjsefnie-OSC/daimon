"""Reactions on the asker's message: 👀 once a turn is accepted, ✅ once answered, ⌛ while queued.

Bot Framework rate-limits a bot's reactions to two a second across every
conversation, answering 429 past that. A reaction is a nicety, so none may fail
or delay a turn or intake: each call runs in the background, spaced to that
limit in this process, and one that would wait longer than `MAX_WAIT_S` for its
slot is dropped rather than queued behind a burst. Calls on one message run in
order, so a removal never overtakes the add it undoes. A failure, a 429
included, is logged and dropped; nothing retries it.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, Final, Protocol

import structlog
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.core.turn.lifecycle import Acknowledgment

log = structlog.get_logger(__name__)

# Teams' reaction ids: the code point, then the emoji's name.
EYES: Final = "1f440_eyes"
CHECK: Final = "2705_whiteheavycheckmark"
HOURGLASS: Final = "231b_hourglassdone"

MIN_INTERVAL_S = 0.5
MAX_WAIT_S = 5.0
CALL_TIMEOUT_S = 5.0


# Activity ids are unique only within a conversation, as the app's delivery dedupe keys them.
MessageKey = tuple[str, str]


@dataclass(frozen=True)
class ReactionTarget:
    """A message a person sent, where Bot Framework can react to it."""

    conversation_id: str
    activity_id: str
    service_url: str | None

    @property
    def key(self) -> MessageKey:
        return (self.conversation_id, self.activity_id)


def reaction_target(inbound: TeamsInbound) -> ReactionTarget | None:
    """The message `inbound` answers; None for a submitted form or a reply nobody addressed."""
    if inbound.from_form or inbound.unprompted:
        return None
    return ReactionTarget(inbound.conversation_id, inbound.activity_id, inbound.service_url)


class React(Protocol):
    """Adds (`add`) or removes one of the bot's reactions on `target`."""

    def __call__(self, target: ReactionTarget, reaction: str, *, add: bool) -> Awaitable[None]: ...


Spawn = Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]


class TeamsReactions:
    """Background, rate-spaced reactions, with the ⌛ marks still owed a removal.

    Without `react` (no Bot Framework client) every method does nothing.
    """

    def __init__(
        self,
        react: React | None,
        *,
        spawn: Spawn,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._react = react
        self._spawn = spawn
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        # The last call queued per message, which the next one on it waits for.
        self._chains: dict[MessageKey, asyncio.Task[None]] = {}
        # Messages showing ⌛.
        self._queued: dict[MessageKey, ReactionTarget] = {}

    def acknowledger(self, inbound: TeamsInbound) -> Callable[[Acknowledgment], None] | None:
        """The lifecycle's `acknowledge` for a turn answering `inbound`, if it can react."""
        if self._react is None or (target := reaction_target(inbound)) is None:
            return None
        return functools.partial(self.acknowledge, target)

    def acknowledge(self, target: ReactionTarget, phase: Acknowledgment) -> None:
        """👀 when the turn is accepted; ✅ in its place once it answered."""
        if phase == "accepted":
            self._queue(target, EYES, add=True)
            return
        self._queue(target, CHECK, add=True)
        self._queue(target, EYES, add=False)

    def mark_queued(self, inbound: TeamsInbound) -> None:
        """⌛ on a message waiting behind the turn running in its chat."""
        if self._react is None or (target := reaction_target(inbound)) is None:
            return
        self._queued[target.key] = target
        self._queue(target, HOURGLASS, add=True)

    def clear_queued(self, items: Iterable[TeamsInbound]) -> None:
        """Remove the ⌛ from these messages, if they show one: their work started or settled."""
        for item in items:
            for activity_id in item.message_ids:
                key = (item.conversation_id, activity_id)
                if (target := self._queued.pop(key, None)) is not None:
                    self._queue(target, HOURGLASS, add=False)

    def _queue(self, target: ReactionTarget, reaction: str, *, add: bool) -> None:
        react = self._react
        if react is None:
            return
        key = target.key
        task = self._spawn(self._run(react, self._chains.get(key), target, reaction, add=add))
        self._chains[key] = task

        def forget(done: asyncio.Task[None]) -> None:
            if self._chains.get(key) is done:
                del self._chains[key]

        task.add_done_callback(forget)

    async def _run(
        self,
        react: React,
        previous: asyncio.Task[None] | None,
        target: ReactionTarget,
        reaction: str,
        *,
        add: bool,
    ) -> None:
        if previous is not None:
            await asyncio.wait([previous])  # never raises: its outcome is its own
        now = self._clock()
        slot = max(now, self._next_slot)
        if slot - now > MAX_WAIT_S:
            log.debug("teams.reaction.skipped", reaction=reaction, add=add, reason="rate")
            return
        self._next_slot = slot + MIN_INTERVAL_S
        if slot > now:
            await self._sleep(slot - now)
        try:
            async with asyncio.timeout(CALL_TIMEOUT_S):
                await react(target, reaction, add=add)
        except (*TEAMS_SEND_ERRORS, TimeoutError) as err:
            status = getattr(getattr(err, "response", None), "status_code", None)
            log.debug(
                "teams.reaction.failed",
                reaction=reaction,
                add=add,
                status=status,
                error=type(err).__name__,
            )
