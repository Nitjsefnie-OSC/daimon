"""Bind actual Slack/Teams post windows to independently acknowledged catalog roots."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.lifecycle import Acknowledgment, InterruptSource, ReconnectReason, acknowledge
from daimon.core.turn.state import TurnState
from daimon.testing.outcome_oracle import TurnEvidence, VisibleText
from daimon.testing.platform_surface import PlatformMessageCapture, PlatformSurfaceCapture
from daimon.testing.slack_surface import SlackSurfaceHarness
from daimon.testing.teams_surface import TeamsSurfaceHarness

if __package__:
    from .discord_catalog_surface import CatalogLifecycle as DiscordCatalogLifecycle
else:
    from discord_catalog_surface import CatalogLifecycle as DiscordCatalogLifecycle


class CatalogLifecycle(DiscordCatalogLifecycle):
    """Preserve Discord/host recording and forward new platforms to real adapters."""

    def __init__(
        self, *, surface: str, evidence_id: str, session_id: str, root_turn_id: str, model_id: str
    ) -> None:
        super().__init__(
            surface=surface,
            evidence_id=evidence_id,
            session_id=session_id,
            root_turn_id=root_turn_id,
            model_id=model_id,
        )
        self.platform_harness: SlackSurfaceHarness | TeamsSurfaceHarness | None = None
        if surface == "slack":
            # The catalog's ordinary table case preserves the adapter's default.
            self.platform_harness = SlackSurfaceHarness(
                evidence_id=evidence_id,
                session_id=session_id,
                root_turn_id=root_turn_id,
                model_id=model_id,
                render_tables=False,
            )
        elif surface == "teams":
            self.platform_harness = TeamsSurfaceHarness(
                evidence_id=evidence_id,
                session_id=session_id,
                root_turn_id=root_turn_id,
                model_id=model_id,
            )

    @property
    def platform_capture(self) -> PlatformSurfaceCapture | None:
        return self.platform_harness.snapshot() if self.platform_harness is not None else None

    async def run(self, invoke: Callable[[], Awaitable[None]]) -> None:
        if self.platform_harness is None:
            await super().run(invoke)
        else:

            async def delivered(_lifecycle: object) -> None:
                if self.platform_harness is None:
                    raise ValueError("platform capture disappeared")
                await self.platform_harness.lifecycle.post_initial()
                await invoke()

            await self.platform_harness.run(delivered)

    async def on_render(self, state: TurnState) -> None:
        await super().on_render(state)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_render(state)

    async def on_terminal_success(self, state: TurnState) -> None:
        await super().on_terminal_success(state)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_terminal_success(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await super().on_terminal_failure(state, err)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_terminal_failure(state, err)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await super().on_sse_event(event)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_sse_event(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await super().on_reconnect(reason)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await super().on_rate_limited(until)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await super().on_interrupt_sent(source)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_interrupt_sent(source)

    async def on_acknowledgment(self, phase: Acknowledgment) -> None:
        await super().on_acknowledgment(phase)
        if self.platform_harness is not None:
            await acknowledge(self.platform_harness.lifecycle, phase)


def bind_platform_text(
    evidence: TurnEvidence,
    capture: PlatformSurfaceCapture | None,
    *,
    expected_id: str,
    expected_platform: str,
    observed_s: float,
    accounting_required: bool = False,
) -> TurnEvidence:
    """Validate full post history and bind surviving text; never bind fallbacks.

    Completion of adapter delivery is independent of native turn completion.
    Accounting-dependent assertions remain partial without real footer evidence.
    Explicit refusals remain effective under python -O.
    """
    if capture is None:
        return evidence
    # model_copy can bypass validators; refuse corrupted serialized receipts too.
    capture = PlatformSurfaceCapture.model_validate_json(capture.model_dump_json())
    if (
        capture.platform != expected_platform
        or capture.evidence_id != expected_id
        or capture.session_id != evidence.session_id
        or capture.root_turn_id != evidence.root_turn_id
        or capture.started_s < evidence.started_s
        or capture.observed_s > observed_s
    ):
        raise ValueError("platform surface differs from the acknowledged root window")
    clocks = [event.message.observed_s for event in capture.events]
    destinations = {(m.destination_id, m.thread_id) for m in capture.messages} | {
        (event.message.destination_id, event.message.thread_id) for event in capture.events
    }
    if clocks != sorted(clocks) or len(destinations) > 1:
        raise ValueError("platform surface has inconsistent destination or clock order")
    survivors: dict[str, PlatformMessageCapture] = {}
    created: set[str] = set()
    for index, event in enumerate(capture.events):
        message = event.message
        if event.evidence_id != f"{expected_id}:post:{index}":
            raise ValueError("platform surface has inconsistent post identity or order")
        if event.operation == "send":
            if message.message_id in created or message.order != len(created):
                raise ValueError("platform surface has duplicate or unordered message creation")
            created.add(message.message_id)
        elif message.message_id not in survivors:
            raise ValueError("platform edit/delete refers to an unowned message")
        elif message.order != survivors[message.message_id].order:
            raise ValueError("platform edit/delete changes message order")
        if event.operation == "delete":
            if message.model_dump(exclude={"observed_s"}) != survivors[
                message.message_id
            ].model_dump(exclude={"observed_s"}):
                raise ValueError("platform delete differs from its owned message")
            del survivors[message.message_id]
            continue
        survivors[message.message_id] = message
    if capture.messages != tuple(survivors.values()):
        raise ValueError("platform surviving text differs from actual post history")
    texts: list[VisibleText] = []
    # Historical drafts/retired controls remain diagnostic events, matching
    # Discord's final surviving-post text domain. Observe the surviving snapshot
    # at the window close, preserving message creation order across replacements.
    for message in capture.messages:
        text = "\n".join(
            part
            for part in (*message.body_texts, *message.card_texts, *message.control_labels)
            if part
        )
        if text.strip():
            texts.append(
                VisibleText(
                    evidence_id=f"{expected_id}:message:{message.message_id}",
                    message_id=message.message_id,
                    order=message.order,
                    observed_s=capture.observed_s,
                    text=text,
                )
            )
    # The oracle owns other effect streams. Refuse order collisions instead of
    # renumbering independently bound cards, approvals or tool effects.
    return TurnEvidence.model_validate(
        {
            **evidence.model_dump(),
            "texts": tuple(texts),
            "text_capture_complete": capture.text_capture_complete and not accounting_required,
        }
    )
