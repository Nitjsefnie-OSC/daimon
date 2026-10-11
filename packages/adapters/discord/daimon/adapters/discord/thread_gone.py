"""A turn whose own thread was deleted or closed under it (#809).

Two signals end a turn's right to write to its thread:

* **gone**: Discord answered a write with code 10003 (Unknown Channel), or the
  gateway reported the thread deleted. Nobody can read the answer, and the
  thread's sessions must not be revived.
* **closed**: a write was answered with code 50083 (thread archived) and the
  archive-and-retry the transport already does could not recover it. The
  thread can be unarchived and mentioned again, so its sessions stay live.

Match on the code, never the status: production saw 10003 as a 400 on an
edit and as a 404 on a send. 10015 (Unknown Webhook) is not here on purpose:
the webhook can vanish while the thread lives, and webhook recovery owns it.

`ThreadStops` is the one per-thread record both the card lifecycle and the bot
consult. The first signal for a thread wins: it logs once and sets the Stop
event of every turn watching the thread, so the driver takes its normal
interrupt path. Later signals only refine the record (closed becomes gone).
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


class ThreadStopped(discord.ClientException):
    """A write was refused because the turn's thread is gone or closed.

    A `ClientException` so the card paths that already tolerate a failed
    delete keep tolerating it; never an `HTTPException`, so nothing mistakes
    it for a Discord answer.
    """


def stop_reason_of(exc: BaseException) -> tuple[StopReason, int] | None:
    """The stop a Discord error means for the thread it was written to, or None."""
    if isinstance(exc, discord.HTTPException):
        if exc.code == GONE_CODE:
            return "gone", GONE_CODE
        if exc.code == CLOSED_CODE:
            return "closed", CLOSED_CODE
    cause = exc.__cause__
    return stop_reason_of(cause) if cause is not None else None


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

    def get(self, thread_id: int) -> ThreadStop | None:
        return self._stops.get(thread_id)

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
    ) -> bool:
        """Record a stop. True for the first signal of this thread, which logs and interrupts."""
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

    def forget(self, thread_id: int) -> None:
        """Drop the thread's record once nothing holds it any more."""
        self._stops.pop(thread_id, None)
        self._cancels.pop(thread_id, None)


class TurnThreadWatch:
    """One turn's view of `ThreadStops`, handed to its transport and card lifecycle."""

    def __init__(self, stops: ThreadStops, thread_id: int) -> None:
        self._stops = stops
        self.thread_id = thread_id
        self.session_id: str | None = None
        self.phase: str = "setup"

    def is_stopped(self) -> bool:
        return self._stops.get(self.thread_id) is not None

    def note_error(self, exc: BaseException) -> None:
        """Signal the stop a failed write to the turn's thread means, if it means one."""
        stop = stop_reason_of(exc)
        if stop is None:
            return
        reason, code = stop
        self._stops.signal(
            self.thread_id,
            reason=reason,
            code=code,
            phase=self.phase,
            session_id=self.session_id,
        )
