"""Read-only rendering context for a Discord mention's admitted slot ticket."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from daimon.core.turn_queue import TurnQueue, TurnTicket

_ticket: ContextVar[TurnTicket | None] = ContextVar("discord_queue_card_ticket", default=None)


@contextmanager
def queue_card_ticket(ticket: TurnTicket) -> Iterator[None]:
    """Observe this task's original ticket without claiming or releasing it."""
    token = _ticket.set(ticket)
    try:
        yield
    finally:
        _ticket.reset(token)


def tenant_slot_waiter(queue: TurnQueue, tenant_id: uuid.UUID) -> Callable[[], bool]:
    """Only this mention's actual tenant-cap wait may change its card."""
    ticket = _ticket.get()

    def waiting() -> bool:
        return (
            ticket is not None
            and ticket.tenant_id == tenant_id
            and ticket.queued
            and ticket.cap is not None
            and queue.in_flight(tenant_id) >= ticket.cap
        )

    return waiting
