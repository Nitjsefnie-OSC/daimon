"""A turn whose own thread was deleted or closed under it (#809).

Two signals end a turn's right to write to its thread:

* **gone**: Discord answered a write with code 10003 (Unknown Channel), or the
  gateway reported the thread deleted. Nobody can read the answer, and the
  thread's sessions must not be revived.
* **closed**: a SEND was answered with code 50083 (thread archived) after the
  transport's own unarchive-and-retry also failed, so the thread cannot be
  written to. The thread can be unarchived and mentioned again, so its
  sessions stay live. An EDIT or delete answered with 50083 is not a stop: a
  thread can auto-archive during a slow turn, and the final send unarchives it
  and delivers the answer.

Match on the code, never the status: production saw 10003 as a 400 on an
edit and as a 404 on a send. 10015 (Unknown Webhook) is not here on purpose:
the webhook can vanish while the thread lives, and webhook recovery owns it.

`ThreadStops` is the one per-thread record both the card lifecycle and the bot
consult. The first signal for a thread wins: it logs once and sets the Stop
event of every turn watching the thread, so the driver takes its normal
interrupt path. A later signal only refines the record (closed becomes gone).

A record outlives its turn's release, so a reaction call suspended across the
release still sees the thread lost. It is cleared only when a new turn claims
the thread and only if it is `closed`: a gone thread stays gone. Each claim
starts a new generation, and a turn's watch remembers the one it was made in:
a late write error from an earlier turn may still report `gone` (deletion is a
fact about the thread) but never `closed`, which is a fact about that turn's
write.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final, Literal

import structlog

import discord

log = structlog.get_logger(__name__)

GONE_CODE: Final[int] = 10003
CLOSED_CODE: Final[int] = 50083

StopReason = Literal["gone", "closed"]
Write = Literal["send", "edit", "delete"]


class ThreadStopped(discord.ClientException):
    """A write was refused because the turn's thread is gone or closed.

    A `ClientException` so the card paths that already tolerate a failed
    delete keep tolerating it; never an `HTTPException`, so nothing mistakes
    it for a Discord answer.
    """


def stop_reason_of(
    exc: BaseException, *, write: Write | None = None
) -> tuple[StopReason, int] | None:
    """The stop a Discord error means for the thread it was written to, or None.

    `write` is the kind of write that failed. Only a send ends `closed`; a
    failure of unknown kind (a turn failure) can only mean `gone`.
    """
    if isinstance(exc, discord.HTTPException):
        if exc.code == GONE_CODE:
            return "gone", GONE_CODE
        if exc.code == CLOSED_CODE and write == "send":
            return "closed", CLOSED_CODE
    cause = exc.__cause__
    return stop_reason_of(cause, write=write) if cause is not None else None


@dataclass
class ThreadStop:
    reason: StopReason
    #: The Discord error code that raised it; None when the gateway said so.
    code: int | None


class ThreadStops:
    """Threads whose running turn must stop writing, and the Stop events to set."""

    def __init__(self) -> None:
        self._stops: dict[int, ThreadStop] = {}
        self._cancels: dict[int, list[asyncio.Event]] = {}
        self._generations: dict[int, int] = {}
        self._next_generation = 0

    def get(self, thread_id: int) -> ThreadStop | None:
        return self._stops.get(thread_id)

    def generation(self, thread_id: int) -> int:
        """The generation of the turn now holding the thread; 0 when none does."""
        return self._generations.get(thread_id, 0)

    def claim(self, thread_id: int) -> None:
        """A new turn takes the thread: start a new generation, forget a `closed` stop.

        A gone thread stays gone. Earlier turns' Stop events are dropped with a
        closed stop: those turns are over.
        """
        self._next_generation += 1
        self._generations[thread_id] = self._next_generation
        stop = self._stops.get(thread_id)
        if stop is not None and stop.reason == "closed":
            del self._stops[thread_id]
            self._cancels.pop(thread_id, None)
        elif stop is None:
            self._cancels.pop(thread_id, None)

    def release(self, thread_id: int) -> None:
        """The turn is over. A stop record stays; the bookkeeping of a healthy thread goes."""
        if thread_id not in self._stops:
            self._generations.pop(thread_id, None)
            self._cancels.pop(thread_id, None)

    def watch(self, thread_id: int, cancel: asyncio.Event) -> None:
        """Set `cancel` on a stop, now if the thread has already stopped."""
        self._cancels.setdefault(thread_id, []).append(cancel)
        if thread_id in self._stops:
            cancel.set()

    def signal(
        self,
        thread_id: int,
        *,
        reason: StopReason,
        code: int | None,
        phase: str,
        session_id: str | None = None,
        generation: int | None = None,
    ) -> bool:
        """Record a stop. True for the first signal of this thread, which logs and interrupts.

        A `closed` signal from a generation that no longer holds the thread is
        dropped: it describes an earlier turn's write, not the current turn.
        """
        if (
            reason == "closed"
            and generation is not None
            and generation != self.generation(thread_id)
        ):
            return False
        existing = self._stops.get(thread_id)
        if existing is not None:
            if reason == "gone":
                existing.reason = "gone"  # a closed thread that is then deleted is gone
            return False
        self._stops[thread_id] = ThreadStop(reason=reason, code=code)
        log.warning(
            "turn.thread_gone",
            thread_id=str(thread_id),
            session_id=session_id,
            reason=reason,
            error_code=code,
            source="gateway" if code is None else "write",
            phase=phase,
        )
        for cancel in self._cancels.get(thread_id, ()):
            cancel.set()
        return True


class TurnThreadWatch:
    """One turn's view of `ThreadStops`, handed to its transport and card lifecycle."""

    def __init__(self, stops: ThreadStops, thread_id: int) -> None:
        self._stops = stops
        self.thread_id = thread_id
        self._generation = stops.generation(thread_id)
        self.session_id: str | None = None
        self.phase: str = "setup"

    def is_stopped(self) -> bool:
        return self._stops.get(self.thread_id) is not None

    def note_error(self, exc: BaseException, *, write: Write) -> None:
        """Signal the stop a failed write to the turn's thread means, if it means one."""
        stop = stop_reason_of(exc, write=write)
        if stop is None:
            return
        reason, code = stop
        self._stops.signal(
            self.thread_id,
            reason=reason,
            code=code,
            phase=self.phase,
            session_id=self.session_id,
            generation=self._generation,
        )
