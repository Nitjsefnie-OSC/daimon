"""`RunningTurns` -- the process's registry of turns currently executing.

`run_prepared_turn` registers the task it runs in for the whole call, so every
entry point that reaches it (a mention, a reply-to-continue button, a wizard
submit, a direct message) is counted without any bookkeeping of its own. A
shutdown drain reads it to know what is still running and to cancel what is
left after its grace window.

One instance lives on `TurnDeps`, so it is shared by every turn the adapter
process runs. No I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator


class RunningTurns:
    """Tasks inside `run_prepared_turn`, keyed by task; a task may nest entries."""

    def __init__(self) -> None:
        self._entries: dict[asyncio.Task[object], int] = {}

    @contextlib.contextmanager
    def track(self) -> Iterator[None]:
        """Count the current task as running a turn until the block exits."""
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - only reachable outside an event loop
            yield
            return
        self._entries[task] = self._entries.get(task, 0) + 1
        try:
            yield
        finally:
            remaining = self._entries.get(task, 0) - 1
            if remaining > 0:
                self._entries[task] = remaining
            else:
                self._entries.pop(task, None)

    def tasks(self) -> set[asyncio.Task[object]]:
        """The tasks still running a turn. A finished task is never reported."""
        return {task for task in self._entries if not task.done()}

    def count(self) -> int:
        return len(self.tasks())
