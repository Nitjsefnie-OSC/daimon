"""The `memory mine` / `memory forget-mine` logic shared by Discord, Slack and Teams."""

from __future__ import annotations

import pytest
from anthropic import AsyncAnthropic
from daimon.core.memory_view import (
    MemoryRequest,
    PersonalMemoryReply,
    forget_code,
    is_strict_memory_args,
    parse_memory_args,
    personal_memory_reply,
)
from daimon.testing.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, MemoryRequest("shared_list")),
        ("", MemoryRequest("shared_list")),
        ("/notes/a.md", MemoryRequest("shared_show", "/notes/a.md")),
        ("mine", MemoryRequest("mine_list")),
        ("MINE /style.md", MemoryRequest("mine_show", "/style.md")),
        ("forget-mine", MemoryRequest("forget_ask")),
        ("forget-mine abc123", MemoryRequest("forget_confirm", "abc123")),
    ],
)
def test_parse_memory_args(text: str | None, expected: MemoryRequest) -> None:
    assert parse_memory_args(text) == expected


@pytest.mark.parametrize(
    ("text", "strict"),
    [
        ("/notes/a.md", True),
        ("mine", True),
        ("mine /a.md", True),
        ("mine is the best agent", False),
        ("forget-mine", True),
        ("forget-mine abc123", True),
        ("forget-mine everything please", False),
        ("what do you remember", False),
    ],
)
def test_strict_args_tell_a_command_from_prose(text: str, strict: bool) -> None:
    assert is_strict_memory_args(text) is strict


async def _store(client: AsyncAnthropic, files: dict[str, str]) -> str:
    store = await client.beta.memory_stores.create(name="personal x", description="d")
    for path, content in files.items():
        await client.beta.memory_stores.memories.create(store.id, path=path, content=content)
    return store.id


async def _reply(
    client: AsyncAnthropic, store_id: str | None, request: MemoryRequest
) -> PersonalMemoryReply:
    return await personal_memory_reply(
        client, agent_name="daimon", store_id=store_id, request=request, command="/memory"
    )


async def test_forget_needs_the_code_it_was_shown_and_touches_no_other_store() -> None:
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    mine = await _store(client, {"/style.md": "tabs", "/tone.md": "brief"})
    theirs = await _store(client, {"/style.md": "spaces"})

    asked = await _reply(client, mine, MemoryRequest("forget_ask"))
    assert len(state.memories[mine]) == 2, "asking deletes nothing"
    code = forget_code(mine, ["/style.md", "/tone.md"])
    assert asked.hint is not None and code in asked.hint

    wrong = await _reply(
        client, mine, MemoryRequest("forget_confirm", forget_code(theirs, ["/style.md"]))
    )
    assert wrong.title.startswith("Nothing was deleted")
    assert len(state.memories[mine]) == 2

    done = await _reply(client, mine, MemoryRequest("forget_confirm", code))
    assert done.title.startswith("Deleted 2 files")
    assert state.memories[mine] == []
    assert len(state.memories[theirs]) == 1, "another store is never touched"


async def test_a_code_goes_stale_when_the_memory_changes() -> None:
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    mine = await _store(client, {"/style.md": "tabs"})
    code = forget_code(mine, ["/style.md"])
    await client.beta.memory_stores.memories.create(mine, path="/new.md", content="x")

    reply = await _reply(client, mine, MemoryRequest("forget_confirm", code))

    assert reply.title.startswith("Nothing was deleted")
    assert len(state.memories[mine]) == 2


async def test_list_and_show_read_the_given_store() -> None:
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    mine = await _store(client, {"/style.md": "tabs"})

    listed = await _reply(client, mine, MemoryRequest("mine_list"))
    shown = await _reply(client, mine, MemoryRequest("mine_show", "/style.md"))
    none = await _reply(client, None, MemoryRequest("mine_list"))

    assert listed.paths == ("/style.md",)
    assert shown.content == "tabs"
    assert none.title == "You have no personal memory with daimon."
