"""Independent review #3 (sidecar-b @ bce1c2fd7): I1/I2 bypass attempts. Secure assertions."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from daimon.adapters.mcp.tools import _session_gate
from daimon.adapters.mcp.tools.discord._read import _history_page
from daimon.adapters.mcp.tools.watches import (
    create_watch_impl,
    manage_watch_impl,
    rotate_watch_impl,
)
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from .test_watch_polling import attended_owner, runtime_watch  # noqa: F401


def _history(messages):
    async def iterate(**kwargs):
        for m in messages[: kwargs["limit"]]:
            yield m

    return SimpleNamespace(history=iterate)


def _msg(identity, *, bot=False, webhook_id=None):
    return SimpleNamespace(
        id=identity,
        author=SimpleNamespace(id=identity, name="a", bot=bot),
        channel=SimpleNamespace(id=4343),
        content=f"m{identity}",
        created_at=datetime.now(UTC),
        attachments=[],
        type=discord.MessageType.default,
        webhook_id=webhook_id,
    )


async def test_i1_webhook_post_is_excluded_even_without_bot_flag():
    """Daimon answers through webhooks; exclusion must not rest on one inferred flag."""
    rows, _, _ = await _history_page(
        _history([_msg(2, webhook_id=777), _msg(1)]),
        limit=1,
        before=None,
        poll_humans_only=True,
    )
    assert [r.id for r in rows] == ["1"]


@pytest.mark.fresh_schema
async def test_i2_shared_slot_unsupported_profile_refuses(attended_owner, monkeypatch):  # noqa: F811
    """Admission reads the shared slot first on ever-shared threads; so must I2."""
    runtime, _, auth, origin = attended_owner
    monkeypatch.setattr(_session_gate, "session_card_gap", AsyncMock(return_value=None))

    async def binding(db, slot, **kw):
        return SimpleNamespace(profile="gemini.inline_reuse") if slot.account_id is None else None

    monkeypatch.setattr("daimon.adapters.mcp.tools.watches.mux_state.get_binding", binding)
    with pytest.raises(ToolError):
        await create_watch_impl(
            runtime,
            auth,
            origin_context_id=origin,
            source="poll",
            instructions="report",
            arguments={"thread_id": "4545"},
        )


@pytest.mark.fresh_schema
@pytest.mark.parametrize("approved", ["resume_watch", "rotate_watch_key", "add_skill"])
async def test_i2_proof_for_another_tool_cannot_create(attended_owner, monkeypatch, approved):  # noqa: F811
    runtime, _, auth, origin = attended_owner

    async def proof(runtime, auth, origin, *, tool_name, **kw):
        return None if tool_name == approved else "session_not_gated"

    monkeypatch.setattr(_session_gate, "session_card_gap", proof)
    with pytest.raises(ToolError):
        await create_watch_impl(
            runtime,
            auth,
            origin_context_id=origin,
            source="poll",
            instructions="report",
            arguments={"thread_id": "4545"},
        )


@pytest.mark.fresh_schema
@pytest.mark.parametrize("action", ["resume", "rotate"])
async def test_i2_proof_lookup_does_not_hold_global_watch_lock(attended_owner, monkeypatch, action):  # noqa: F811
    """The card proof makes provider calls; the global firing lock must not be held meanwhile."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    runtime, watch, auth, origin = attended_owner
    if action == "rotate":
        async with runtime.session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET source='page_event' WHERE id=:id"), {"id": watch.id}
            )
    await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="pause")
    independent = async_sessionmaker(runtime.session_factory.kw["bind"].engine)
    observed = {}

    async def proof(*a, **k):
        async with independent() as other:
            observed["free"] = await other.scalar(text("SELECT pg_try_advisory_lock(813749201)"))
            if observed["free"]:
                await other.execute(text("SELECT pg_advisory_unlock(813749201)"))
        return None

    monkeypatch.setattr(_session_gate, "session_card_gap", proof)
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools.watches.private_key_delivery", AsyncMock(return_value=None)
    )
    try:
        if action == "resume":
            await manage_watch_impl(
                runtime, auth, watch_id=str(watch.id), action="resume", origin_context_id=origin
            )
        else:
            await rotate_watch_impl(runtime, auth, watch_id=str(watch.id), origin_context_id=origin)
    except ToolError:
        pass
    assert observed.get("free") is True
