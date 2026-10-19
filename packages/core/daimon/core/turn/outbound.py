"""Keep another chat app's name out of what a turn posts on a chat platform.

`run_prepared_turn` wraps the adapter's lifecycle so the turn state it renders, the
final answer and any sealed answer reach a platform with the other apps' names already
replaced (`scrub_other_platforms`). The adapters stay unaware, and a lifecycle added for
a fourth platform inherits the rule. The wrapper only touches what the adapter is
handed: the driver, the transcript and the run outcome keep the model's own words.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import datetime
from typing import Any

from anthropic.types import RawMessageStreamEvent
from daimon.core.platform_words import scrub_other_platforms
from daimon.core.stores.domain import CHAT_PLATFORMS
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.state import ContentBlock, TextBlock, TurnState


def scrub_turn_state(state: TurnState, platform: str | None) -> TurnState:
    """`state` with the model's text scrubbed for `platform`; `state` itself when nothing changes.

    Tool-call blocks are left alone: their names and results are not posted as written."""
    changed = False
    content: list[ContentBlock] = []
    for block in state.content:
        if isinstance(block, TextBlock):
            text = scrub_other_platforms(block.text, platform)
            if text != block.text:
                block = dataclasses.replace(block, text=text)
                changed = True
        content.append(block)
    return dataclasses.replace(state, content=content) if changed else state


class OtherAppsScrubbedLifecycle:
    """A `TurnLifecycle` that hands `inner` scrubbed turn state.

    Hooks the protocol leaves optional (`on_acknowledgment`, `on_provider_event`,
    `on_render_stopped`) are looked up on `inner`, so they exist here only when they exist there."""

    def __init__(self, inner: TurnLifecycle, platform: str) -> None:
        self._inner = inner
        self._platform = platform

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._inner, name)

    async def on_render(self, state: TurnState) -> None:
        await self._inner.on_render(scrub_turn_state(state, self._platform))

    async def on_terminal_success(self, state: TurnState) -> None:
        await self._inner.on_terminal_success(scrub_turn_state(state, self._platform))

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await self._inner.on_terminal_failure(scrub_turn_state(state, self._platform), err)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await self._inner.on_sse_event(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await self._inner.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await self._inner.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await self._inner.on_interrupt_sent(source)


def scrubbed_for(
    platform: str,
    lifecycle: TurnLifecycle,
    recovery_lifecycle: Callable[[asyncio.Event], TurnLifecycle],
) -> tuple[TurnLifecycle, Callable[[asyncio.Event], TurnLifecycle]]:
    """The turn's lifecycle and its recovery lifecycle, scrubbed when `platform` is a chat app."""
    if platform not in CHAT_PLATFORMS:
        return lifecycle, recovery_lifecycle
    return (
        OtherAppsScrubbedLifecycle(lifecycle, platform),
        lambda cancel: OtherAppsScrubbedLifecycle(recovery_lifecycle(cancel), platform),
    )
