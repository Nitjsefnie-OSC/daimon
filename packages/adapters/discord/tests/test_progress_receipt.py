"""Initial receipts follow actual discord.py posts, never callback bookkeeping."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any
from uuid import UUID

import discord
import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.progress_receipt import InitialProgressReceipt
from daimon.adapters.discord.views import CancelView
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.discord_surface import DiscordSurfaceHarness
from pydantic import ValidationError


@pytest.fixture
async def harness() -> DiscordSurfaceHarness:
    host = DiscordSurfaceHarness(
        evidence_id="initial-wire",
        session_id="unit-session",
        root_turn_id="unit-root",
        clock=lambda: 100.0,
    )
    return host


def lifecycle(
    harness: DiscordSurfaceHarness,
    receipts: list[InitialProgressReceipt] | None,
    **kwargs: Any,
) -> DiscordTurnLifecycle:
    view = kwargs.pop("cancel_view", None) or CancelView(
        allowed_user_id=103, cancel=asyncio.Event(), turn_id=UUID(int=1)
    )
    return DiscordTurnLifecycle(
        send=harness.transport.send,
        edit=harness.transport.edit,
        delete=harness.transport.delete,
        agent_name="Daimon",
        model_id="offline",
        clock=lambda: 100.0,
        cancel_view=view,
        on_initial_progress=receipts.append if receipts is not None else None,
        **kwargs,
    )


async def test_receipt_joins_actual_wire_and_public_card_reference(
    harness: DiscordSurfaceHarness,
) -> None:
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(harness, receipts)
    await adapter.post_initial()
    assert len(receipts) == len(harness.gateway.events) == 1
    receipt, post = receipts[0], harness.gateway.events[0]
    assert adapter.message_ref is not None
    assert receipt.message_id == post.message.message_id == str(adapter.message_ref.id)
    assert receipt.destination_id == post.message.channel_id == str(harness.thread.id)
    assert receipt.thread_id is None and receipt.operation == post.operation == "send"
    assert receipt.observed_s == post.message.observed_s == 100.0
    rendered, wire = json.loads(receipt.rendered_json), json.loads(post.request_json)
    assert rendered["embeds"] == wire["embeds"]
    assert rendered["components"] == wire["components"]
    assert str(UUID(int=1)) in receipt.rendered_json
    assert receipt.phase == "initial"
    assert InitialProgressReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    assert (
        not {"session_id", "root_turn_id", "accepted", "running"}
        & InitialProgressReceipt.model_fields.keys()
    )


async def test_render_terminal_and_debounce_do_not_reissue_initial_receipt(
    harness: DiscordSurfaceHarness,
) -> None:
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(harness, receipts)
    await adapter.post_initial()
    frozen = receipts[0].model_dump_json()
    await adapter.post_initial()
    await adapter.on_render(TurnState())
    await adapter.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="answer")]))
    assert len(receipts) == 1 and receipts[0].model_dump_json() == frozen
    assert len(harness.gateway.events) > 1


@pytest.mark.parametrize("unprompted", [True, False])
async def test_no_receipt_for_suppressed_or_failed_send(
    harness: DiscordSurfaceHarness,
    unprompted: bool,
) -> None:
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(harness, receipts, unprompted=unprompted)
    if unprompted:
        await adapter.post_initial()
    else:
        harness.gateway.fail_next("send", RuntimeError("offline rejection"))
        with pytest.raises(RuntimeError, match="offline rejection"):
            await adapter.post_initial()
    assert receipts == [] and harness.gateway.events == []


async def test_persistence_retry_cannot_invent_a_second_post(
    harness: DiscordSurfaceHarness,
) -> None:
    receipts: list[InitialProgressReceipt] = []
    attempts = 0

    async def persist(message: discord.Message) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("offline persistence failure")

    adapter = lifecycle(harness, receipts, on_first_post=persist)
    with pytest.raises(RuntimeError, match="persistence failure"):
        await adapter.post_initial()
    assert len(receipts) == 1, "the wire ACK remains a fact even if persistence fails"
    await adapter.post_initial()
    assert len(receipts) == len(harness.gateway.events) == 1 and attempts == 2


async def test_opt_in_does_not_change_serialized_request(
    harness: DiscordSurfaceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nonce(bits: int) -> int:
        return 2

    monkeypatch.setattr(secrets, "randbits", nonce)
    await lifecycle(harness, None).post_initial()
    first = harness.gateway.events[0].request_json
    await lifecycle(harness, []).post_initial()
    assert harness.gateway.events[1].request_json == first


async def test_production_posting_mutation_removes_receipt(
    harness: DiscordSurfaceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_flush(self: DiscordTurnLifecycle, *, initial: bool = False) -> None:
        pass

    monkeypatch.setattr(DiscordTurnLifecycle, "_maybe_flush", broken_flush)
    with pytest.raises(AssertionError):
        await test_receipt_joins_actual_wire_and_public_card_reference(harness)


async def test_adopted_card_receipt_records_real_edit(
    harness: DiscordSurfaceHarness,
) -> None:
    original = lifecycle(harness, None)
    await original.post_initial()
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(harness, receipts, adopt_message_ref=original.message_ref)
    await adapter.post_initial()
    assert len(receipts) == 1 and receipts[0].operation == "edit"
    assert receipts[0].message_id == harness.gateway.events[-1].message.message_id
    assert harness.gateway.events[-1].operation == "edit"


@pytest.mark.parametrize(
    "field,value", [("message_id", ""), ("observed_s", float("nan")), ("root_turn_id", "made-up")]
)
async def test_receipt_is_closed_and_refuses_invalid_json(
    harness: DiscordSurfaceHarness,
    field: str,
    value: object,
) -> None:
    receipts: list[InitialProgressReceipt] = []
    await lifecycle(harness, receipts).post_initial()
    data = receipts[0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        InitialProgressReceipt.model_validate_json(json.dumps(data))


async def test_receipt_waits_for_actual_sdk_ack(
    harness: DiscordSurfaceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    receipts: list[InitialProgressReceipt] = []
    original = harness.gateway.send_message

    async def hold(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(harness.gateway, "send_message", hold)
    view = CancelView(allowed_user_id=103, cancel=asyncio.Event(), turn_id=UUID(int=1))
    adapter = lifecycle(harness, receipts, cancel_view=view)
    task = asyncio.create_task(adapter.post_initial())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not receipts and adapter.message_ref is None and not harness.gateway.events
        view.cancel_button.disabled = True
        release.set()
        await asyncio.wait_for(task, 5)
        assert len(receipts) == len(harness.gateway.events) == 1
        rendered = json.loads(receipts[0].rendered_json)
        wire = json.loads(harness.gateway.events[0].request_json)
        assert rendered["components"] == wire["components"]
        assert rendered["components"][0]["components"][0]["disabled"] is False
        assert view.cancel_button.disabled is True
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
