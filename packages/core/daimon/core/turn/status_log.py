"""The `turn.status_log` switch, read by the reducer, the answer extractors and the card log.

Off (the default), a turn folds, posts and draws a card exactly as before: consecutive
`agent.message` texts are joined into one block, the card shows the latest line, and
the answer is every text after the last tool call. On, each message is its own block,
a short emoji-led line before the answer stays off the answer, and the card keeps the
last few lines.

The value comes from `TurnSettings.status_log` (`DAIMON_TURN__STATUS_LOG`), read once per
process. `status_log_override` lets a test or an embedding caller pin it for a block.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

_OVERRIDE: ContextVar[bool | None] = ContextVar("turn_status_log", default=None)


@lru_cache(maxsize=1)
def _configured() -> bool:
    from daimon.core.config import load_turn_settings

    return load_turn_settings().status_log


def status_log_enabled() -> bool:
    """Whether this turn keeps messages apart and logs recent ones on its card."""
    override = _OVERRIDE.get()
    return override if override is not None else _configured()


@contextmanager
def status_log_override(enabled: bool) -> Iterator[None]:
    """Pin the switch inside the block, whatever the environment says."""
    token = _OVERRIDE.set(enabled)
    try:
        yield
    finally:
        _OVERRIDE.reset(token)
