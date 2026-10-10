"""Explicit, offline host probes over Discord's existing queue and drain paths.

Nothing imports this module in the application. A probe needs a fresh bot and
normal host callbacks; it reports facts, never conformance verdicts. The caller
owns independent expectations, provider choreography and ledger assertions.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from daimon.adapters.discord.bot import DaimonBot

import discord


@dataclass(frozen=True)
class MentionTurn:
    """Exactly the inputs passed by the actual Discord mention/drain path."""

    message: discord.Message
    guild_id: str
    tenant_id: uuid.UUID
    content_override: str | None
    attachments_override: tuple[discord.Attachment, ...] | None


@dataclass(frozen=True)
class AdmissionBarrier:
    """Host callback signals started after actual admission/root acceptance.

    It then waits for release before finishing that root, while the probe
    enqueues follow-ups. Neither event is a provider acceptance receipt.
    """

    started: asyncio.Event
    release: asyncio.Event


@dataclass(frozen=True)
class ThreadInputs:
    first: discord.Message
    queued: tuple[discord.Message, ...]


@dataclass(frozen=True)
class MultiHumanEnvelope:
    bot: DaimonBot
    tenant_id: uuid.UUID
    guild_id: str
    channel_id: int
    threads: tuple[ThreadInputs, ...]
    run: Callable[[MentionTurn, AdmissionBarrier | None], Awaitable[None]]
    timeout_s: float = 30.0


async def multi_human_evidence(envelope: MultiHumanEnvelope) -> Mapping[str, object]:
    """Capture actual queue/drain inputs; normal callbacks retain host ownership.

    The first root is held at the owner-supplied barrier. Follow-ups enter via
    `_queue_behind_inflight_turn`, including its reaction ordering; the actual
    `_drain_pending_mentions` groups/composes/forwards them. Each callback must
    perform ordinary admit/bind/run, not supply a pass/fail result. Ledger,
    workspace, native-root and credential evidence are separate host facts.

    A failed/timeout/cancelled probe restores the handler, cancels its task and
    releases only its own claimed thread. Use a dedicated bot, never a live app.
    """
    bot = envelope.bot
    if (
        not math.isfinite(envelope.timeout_s)
        or envelope.timeout_s <= 0
        or len(envelope.threads) < 2
    ):
        raise ValueError("C01 requires two threads and a finite positive timeout")
    # Do not replace a live handler or reuse an existing queue.
    if bot._processing or bot._pending:  # pyright: ignore[reportPrivateUsage]
        raise ValueError("C01 requires a fresh idle bot")
    seen_threads: set[int] = set()
    seen_messages: set[int] = set()
    humans: set[int] = set()
    for inputs in envelope.threads:
        if (
            not isinstance(inputs.first.channel, discord.Thread)
            or inputs.first.channel.parent_id != envelope.channel_id
            or not inputs.queued
        ):
            raise ValueError("C01 requires thread messages")
        thread_id = inputs.first.channel.id
        if thread_id in seen_threads:
            raise ValueError("C01 requires distinct thread envelopes")
        seen_threads.add(thread_id)
        for message in (inputs.first, *inputs.queued):
            if (
                not isinstance(message.channel, discord.Thread)
                or message.channel.id != thread_id
                or message.channel.parent_id != envelope.channel_id
                or message.guild is None
                or str(message.guild.id) != envelope.guild_id
                or message.author.bot
                or message.id in seen_messages
            ):
                raise ValueError("C01 messages must be unique humans in the specified guild/thread")
            seen_messages.add(message.id)
            humans.add(message.author.id)
    if len(humans) < 2:
        raise ValueError("C01 requires two distinct human callers")

    prior = bot.__dict__.get("_handle_mention")
    batches: list[Mapping[str, object]] = []
    threads: list[Mapping[str, object]] = []
    active: dict[int, int] = {}
    peak: dict[int, int] = {}
    barrier: AdmissionBarrier | None = None
    first_id: int | None = None

    async def handle(
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
    ) -> None:
        del created_thread_ids
        thread_id = message.channel.id
        active[thread_id] = active.get(thread_id, 0) + 1
        peak[thread_id] = max(peak.get(thread_id, 0), active[thread_id])
        batches.append(
            {
                "thread_id": str(thread_id),
                "trigger_message_id": str(message.id),
                "caller_id": str(message.author.id),
                "content": message.content if content_override is None else content_override,
                "content_overridden": content_override is not None,
                "attachment_ids": tuple(
                    str(a.id)
                    for a in (
                        message.attachments
                        if attachments_override is None
                        else attachments_override
                    )
                ),
            }
        )
        try:
            await envelope.run(
                MentionTurn(
                    message,
                    guild_id,
                    tenant_id,
                    content_override,
                    None if attachments_override is None else tuple(attachments_override),
                ),
                barrier if message.id == first_id else None,
            )
        finally:
            active[thread_id] -= 1

    bot._handle_mention = handle  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    try:
        async with asyncio.timeout(envelope.timeout_s):
            for inputs in envelope.threads:
                thread_id = inputs.first.channel.id
                first_id = inputs.first.id
                barrier = AdmissionBarrier(asyncio.Event(), asyncio.Event())
                bot._thread_queue.claim(thread_id)  # pyright: ignore[reportPrivateUsage]
                task = asyncio.create_task(
                    handle(inputs.first, envelope.guild_id, envelope.tenant_id)
                )
                waiter = asyncio.create_task(barrier.started.wait())
                try:
                    # A callback failure must surface immediately rather than
                    # hanging until the barrier deadline.
                    await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
                    if task.done():
                        await task
                        if not barrier.started.is_set() or not inputs.queued:
                            raise RuntimeError("C01 first callback must hold an admitted root")
                        raise RuntimeError("C01 first callback finished before queued inputs")
                    for message in inputs.queued:
                        await bot._queue_behind_inflight_turn(  # pyright: ignore[reportPrivateUsage]
                            thread_id, message
                        )
                    pending = bot._pending.get(thread_id, [])  # pyright: ignore[reportPrivateUsage]
                    threads.append(
                        {
                            "thread_id": str(thread_id),
                            "received_message_ids": tuple(
                                str(m.id) for m in (inputs.first, *inputs.queued)
                            ),
                            "queued_message_ids": tuple(str(m.id) for m in pending),
                            "claimed_while_queued": thread_id in bot._processing,  # pyright: ignore[reportPrivateUsage]
                            "first_callback_finished_while_queued": task.done(),
                        }
                    )
                    barrier.release.set()
                    await task
                    barrier = None
                    await bot._drain_pending_mentions(  # pyright: ignore[reportPrivateUsage]
                        thread_id, envelope.guild_id, envelope.tenant_id
                    )
                finally:
                    if barrier is not None:
                        barrier.release.set()
                    waiter.cancel()
                    task.cancel()
                    await asyncio.gather(task, waiter, return_exceptions=True)
                    bot._processing.discard(thread_id)  # pyright: ignore[reportPrivateUsage]
                    bot._pending.pop(thread_id, None)  # pyright: ignore[reportPrivateUsage]
    finally:
        if prior is None:
            del bot.__dict__["_handle_mention"]
        else:
            bot.__dict__["_handle_mention"] = prior
    return {
        "platform": "discord",
        "threads": tuple(threads),
        "batches": tuple(batches),
        "peak_callbacks_by_thread": {str(key): value for key, value in peak.items()},
        "queue_empty_after": not bot._pending,  # pyright: ignore[reportPrivateUsage]
        "processing_empty_after": not bot._processing,  # pyright: ignore[reportPrivateUsage]
    }
