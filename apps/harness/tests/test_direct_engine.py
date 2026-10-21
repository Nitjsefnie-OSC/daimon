"""The hand-written loop with Azure HTTP mocked below it, then through the real service."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon_harness import direct_engine, model_adapter
from daimon_harness.credentials import turn_credentials
from daimon_harness.direct_engine import DirectAnthropicAdapter, DirectResponsesAdapter
from daimon_harness.errors import HarnessError
from daimon_harness.journal import ConflictError, Journal
from daimon_harness.model_adapter import AzureModelAdapter
from daimon_harness.runtime import TurnContext
from daimon_harness.service import Supervisor
from mux.contracts.actions import UserToolConfirmation, UserToolResult
from mux.contracts.events import TextPart
from mux.drivers.selfhosted.wire import ActionRequest, InlineSkill, MCPServer, TurnRequest

from .conftest import serve_harness, turn
from .direct_support import (
    SECRET,
    Script,
    anthropic_script,
    call_item,
    install_anthropic,
    install_azure,
    mcp_server,
    message_item,
    message_json,
    reasoning_item,
    response_json,
    serving_adapter,
    usage_json,
)
from .interop_support import SCOPE, collect, driver_session, message, workspace
from .live_probe import checked_client, luna_deployment, run_luna_probe

Events = list[tuple[str, dict[str, Any]]]
TEXT = {"type": "text", "text": "look"}
IMAGE = {"type": "image", "media_type": "image/png", "data_b64": "AAAA"}


def recording_context(home: Path, request: TurnRequest | None = None) -> tuple[TurnContext, Events]:
    events: Events = []

    async def emit(kind: str, data: dict[str, Any]) -> None:
        events.append((kind, data))

    return TurnContext(request or TurnRequest.model_validate(turn()), home, emit), events


def kinds(events: Events) -> list[str]:
    return [kind for kind, _ in events]


def outputs_sent_back(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in body["input"] if item.get("type") == "function_call_output"]


# --- Engine level: Azure HTTP mocked below the loop --------------------------------------------


async def test_tool_loop_meters_each_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    journal = Journal(tmp_path)
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    events: Events = []

    async def emit(kind: str, data: dict[str, Any]) -> None:
        journal.append(request, kind, data)
        events.append((kind, data))

    async def before(count: int, body: dict[str, Any]) -> None:
        committed = journal.events("session-1")
        assert committed[-1].type == "model_request_started"
        assert sum(event.type == "model_request_started" for event in committed) == count
        assert body["store"] is False
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["parallel_tool_calls"] is False
        if count == 2:
            assert len(outputs_sent_back(body)) == 1
            reasoning = next(item for item in body["input"] if item.get("type") == "reasoning")
            assert reasoning["encrypted_content"] == "encrypted-reasoning"

    script = Script(
        [reasoning_item(), call_item("write", {"path": "from-loop.txt", "content": "loop"})],
        [message_item("saved")],
        before=before,
    )
    install_azure(monkeypatch, script)
    context = TurnContext(request, tmp_path, emit)
    history = await DirectResponsesAdapter().run(context, [])
    assert (tmp_path / "work/from-loop.txt").read_text() == "loop"
    assert len(script.bodies) == 2
    usages = [data for kind, data in events if kind == "usage"]
    starts = [data for kind, data in events if kind == "model_request_started"]
    assert [data["request_id"] for data in starts] == [data["request_id"] for data in usages]
    assert usages == [
        {
            "request_id": usages[i - 1]["request_id"],
            "model": "gpt-6-luna",
            "input_tokens": 12,
            "cached_input_tokens": 3,
            "output_tokens": 8,
            "reasoning_tokens": 2,
        }
        for i in (1, 2)
    ]
    assert journal.pending_requests(request) == {}
    journal.close()
    assert [item.get("type", item.get("role")) for item in history] == [
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert kinds(events).count("tool_use") == kinds(events).count("tool_result") == 1


async def test_history_and_requests_match_the_sdk_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Same scripted model, both loops: sessions written by one stay valid for the other."""
    outputs = [
        [reasoning_item(), call_item("write", {"path": "a.txt", "content": "x"})],
        [message_item("saved")],
    ]
    request = TurnRequest.model_validate(turn() | {"input": [TEXT, IMAGE]})
    seen: dict[str, tuple[list[dict[str, Any]], list[Any], Events]] = {}
    for name, adapter, module in (
        ("sdk", AzureModelAdapter, model_adapter),
        ("direct", DirectResponsesAdapter, direct_engine),
    ):
        script = Script(*outputs)
        install_azure(monkeypatch, script, module)
        context, events = recording_context(tmp_path / name, request)
        history = await adapter().run(context, [{"role": "user", "content": "earlier"}])
        seen[name] = (script.bodies, history, events)

    def comparable(body: dict[str, Any]) -> dict[str, Any]:
        fields = ("model", "instructions", "input", "tools", "store", "parallel_tool_calls")
        return {field: body[field] for field in fields} | {"include": sorted(body["include"])}

    def stable(events: Events) -> list[Any]:
        ids = {"request_id", "message_id", "action_id"}
        return [(kind, {k: v for k, v in data.items() if k not in ids}) for kind, data in events]

    assert [comparable(b) for b in seen["direct"][0]] == [comparable(b) for b in seen["sdk"][0]]
    assert seen["direct"][1] == seen["sdk"][1]
    assert stable(seen["direct"][2]) == stable(seen["sdk"][2])


async def test_managed_identity_token_reaches_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from .identity_probe import TOKEN, managed_identity_endpoint

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer " + TOKEN
        return httpx.Response(200, json=response_json([message_item()]))

    async with managed_identity_endpoint() as calls:
        install_azure(monkeypatch, handler)
        monkeypatch.delenv("AZURE_OPENAI_API_KEY")
        adapter = DirectResponsesAdapter()
        assert adapter.azure.key is None and adapter.azure.credential is not None
        try:
            context, _ = recording_context(tmp_path)
            await adapter.run(context, [])
            assert len(calls) == 1
        finally:
            await adapter.azure.credential.close()


@pytest.mark.skipif(
    not (
        os.environ.get("AZURE_OPENAI_ENDPOINT")
        and (os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_CLIENT_ID"))
    ),
    reason="requires explicit Azure OpenAI credentials",
)
async def test_live_azure_responses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    body = turn()
    body["model"] = luna_deployment()
    body["input"] = [{"type": "text", "text": "Reply with the word ready. Do not use tools."}]
    monkeypatch.setattr(direct_engine, "AsyncAzureOpenAI", checked_client)
    context, events = recording_context(tmp_path, TurnRequest.model_validate(body))
    await run_luna_probe(DirectResponsesAdapter(), context)  # type: ignore[arg-type]
    assert kinds(events).count("usage") == 1
    messages = [data for kind, data in events if kind == "agent_message"]
    deltas = [data for kind, data in events if kind == "agent_message_delta"]
    assert messages
    assert [data["message_id"] for data in deltas] == [data["message_id"] for data in messages]


async def test_instructions_skills_images_message_ids_and_interrupted_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    request = TurnRequest.model_validate(turn() | {"input": [TEXT, IMAGE]})
    request.skills = [InlineSkill(name="analysis", files={"SKILL.md": "Use the analysis skill"})]
    interrupted = {"role": "user", "content": "Interrupted turn input: [...]"}
    script = Script([message_item("first"), message_item("second", id="msg2")])
    install_azure(monkeypatch, script)
    context, events = recording_context(tmp_path, request)
    history = await DirectResponsesAdapter().run(context, [interrupted])
    body = script.bodies[0]
    assert body["instructions"].startswith("Help\n\n")
    assert "Use the analysis skill" in body["instructions"]
    assert body["input"][0] == interrupted
    assert body["input"][1] == {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
        ],
    }
    assert history[0] == interrupted
    assert context.model_input == body["input"]
    deltas = [data for kind, data in events if kind == "agent_message_delta"]
    finals = [data for kind, data in events if kind == "agent_message"]
    assert [d["text"] for d in deltas] == ["first", "second"]
    assert [d["message_id"] for d in deltas] == [f["message_id"] for f in finals]
    assert len({d["message_id"] for d in deltas}) == 2


async def test_builtin_tool_schemas_match_the_sdk_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context, _ = recording_context(tmp_path)
    sdk = {tool.name: tool for tool in model_adapter.builtin_tools(context)}
    mine = {tool.name: tool for tool in direct_engine.builtin_tools(context)}
    assert list(mine) == list(sdk)
    for name, tool in mine.items():
        assert tool.schema == sdk[name].params_json_schema  # type: ignore[attr-defined]
        assert tool.description == sdk[name].description  # type: ignore[attr-defined]
        assert tool.strict and not tool.mcp


# A response without both integer token counts is unmeasured, never zero usage.
MALFORMED_USAGE: dict[str, object] = {
    "missing_usage": None,
    "empty_usage": {},
    "no_output_tokens": {"input_tokens": 12},
    "null_input_tokens": {"input_tokens": None, "output_tokens": 8},
    "boolean_tokens": {"input_tokens": False, "output_tokens": True, "total_tokens": 1},
}


@pytest.mark.parametrize("failure", ["http", *MALFORMED_USAGE, "cancelled"])
async def test_unmeasured_request_never_invents_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    started = asyncio.Event()
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        if failure == "cancelled":
            await asyncio.Event().wait()
        if failure == "http":
            error = {"message": "private-error", "type": "server_error"}
            return httpx.Response(500, headers={"x-request-id": "failed"}, json={"error": error})
        return httpx.Response(
            200, json=response_json([message_item()], usage=MALFORMED_USAGE[failure])
        )

    install_azure(monkeypatch, respond)
    context, events = recording_context(tmp_path)
    task = asyncio.create_task(DirectResponsesAdapter().run(context, []))
    await asyncio.wait_for(started.wait(), 5)
    if failure == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(HarnessError) as caught:
            await task
        assert caught.value.kind == ("usage_unavailable" if failure == "http" else "usage_missing")
    assert calls == 1  # no retry anywhere
    assert "usage" not in kinds(events)
    error = next(data for kind, data in events if kind == "error")
    assert error["kind"] == "usage_unavailable"
    start = next(data for kind, data in events if kind == "model_request_started")
    assert start["request_id"] == error["request_id"]
    assert "private-error" not in str(events)


@pytest.mark.parametrize("after_tool", [False, True])
async def test_context_overflow_truncates_history_and_inventories_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_tool: bool
):
    requests: list[dict[str, Any]] = []
    overflow_at = 1 + int(after_tool)

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if after_tool and len(requests) == 1:
            write = call_item("write", {"path": "once.txt", "content": "once"}, "write")
            reasoning = reasoning_item("opaque-required-reasoning", "reasoning-for-write")
            return httpx.Response(200, json=response_json([reasoning, write], usage=usage_json()))
        if len(requests) == overflow_at:
            assert "old-history-marker" in str(body["input"])
            error = {"message": "too long", "code": "context_length_exceeded"}
            return httpx.Response(400, json={"error": error | {"type": "invalid_request_error"}})
        assert "old-history-marker" not in str(body["input"])
        if after_tool:
            retained = body["input"]
            reasoning = next(item for item in retained if item.get("type") == "reasoning")
            assert reasoning["encrypted_content"] == "opaque-required-reasoning"
            call = next(item for item in retained if item.get("type") == "function_call")
            result = outputs_sent_back(body)[0]
            assert call["call_id"] == result["call_id"] == "write"
            assert retained.index(reasoning) < retained.index(call) < retained.index(result)
        return httpx.Response(200, json=response_json([message_item("ok")]))

    install_azure(monkeypatch, respond)
    context, events = recording_context(tmp_path)
    history = await DirectResponsesAdapter().run(
        context, [{"role": "user", "content": "old-history-marker"}]
    )
    assert len(requests) == 2 + int(after_tool)
    if after_tool:
        assert context.tool_calls == 1
        assert (context.work / "once.txt").read_text() == "once"
    assert "old-history-marker" not in str(history)
    starts = [data["request_id"] for kind, data in events if kind == "model_request_started"]
    unknown = [
        d["request_id"] for k, d in events if k == "error" and d["kind"] == "usage_unavailable"
    ]
    measured = [data["request_id"] for kind, data in events if kind == "usage"]
    assert len(starts) == 2 + int(after_tool) and len(set(starts)) == len(starts)
    assert set(starts) == set(unknown + measured)
    assert any(k == "error" and d["kind"] == "context_truncated" for k, d in events)


async def test_overflow_recovery_compacts_large_tool_output_and_runs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bash = call_item("bash", {"command": "head -c 20000 /dev/zero | tr '\\0' x"}, "big")
    overflow = httpx.Response(
        400, json={"error": {"message": "x", "code": "context_length_exceeded"}}
    )
    responses = [
        httpx.Response(200, json=response_json([bash])),
        overflow,
        overflow,  # a second overflow is not retried again
    ]
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return responses[len(bodies) - 1]

    install_azure(monkeypatch, respond)
    context, events = recording_context(tmp_path)
    with pytest.raises(HarnessError, match="without usage"):
        await DirectResponsesAdapter().run(context, [])
    assert len(bodies) == 3
    sent = outputs_sent_back(bodies[2])[0]["output"]
    assert sent.endswith("[output truncated for context]") and len(sent) < 9000
    assert len(outputs_sent_back(bodies[1])[0]["output"]) > 20000
    assert kinds(events).count("error") == 3  # two unmeasured requests and one context_truncated


async def test_denied_call_retains_reasoning_and_paired_output_in_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def check_tools(count: int, body: dict[str, Any]) -> None:
        assert "bash" not in [tool["name"] for tool in body["tools"]]
        if count == 2:
            calls = [i for i in body["input"] if i.get("type") == "function_call"]
            results = outputs_sent_back(body)
            assert len(calls) == len(results) == 1
            assert calls[0]["call_id"] == results[0]["call_id"] == "denied-call"
            assert "denied by policy" in results[0]["output"]
            assert any(item.get("type") == "reasoning" for item in body["input"])

    script = Script(
        [
            reasoning_item("opaque-reasoning", "reasoning-1"),
            call_item("bash", '{"command":"touch forbidden"}', "denied-call"),
        ],
        [message_item()],
        [message_item()],
        before=check_tools,
    )
    install_azure(monkeypatch, script)
    request = TurnRequest.model_validate(turn())
    request.tool_policy = {"bash": "deny"}
    first, events = recording_context(tmp_path, request)
    adapter = DirectResponsesAdapter()
    history = await adapter.run(first, [])
    second, _ = recording_context(tmp_path, request.model_copy(update={"turn_id": "second"}))
    await adapter.run(second, history)
    assert len(script.bodies) == 3
    assert not (first.work / "forbidden").exists()
    use = next(data for kind, data in events if kind == "tool_use")
    assert use["input"] == {"arguments": '{"command":"touch forbidden"}'}
    result = next(data for kind, data in events if kind == "tool_result")
    assert result["call_id"] == "denied-call" and result["is_error"] is True


@pytest.mark.parametrize(
    "status,reason",
    [
        ("incomplete", "max_output_tokens"),
        ("incomplete", "content_filter"),
        ("incomplete", None),
        ("failed", "server_error"),
        ("cancelled", None),
        ("in_progress", None),
        ("queued", None),
        (None, None),
    ],
)
@pytest.mark.parametrize("tool_call", [False, True])
async def test_noncompleted_response_preserves_usage_and_ends_errored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
    reason: str | None,
    tool_call: bool,
):
    bad_call = call_item(
        "write", {"path": "must-not-execute", "content": "bad"}, "unsafe", status="incomplete"
    )
    partial_text = message_item("partial", status="incomplete")
    output = [bad_call] if tool_call else [partial_text]
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=response_json(
                [] if status == "failed" and not tool_call else output,
                status=status,
                incomplete_details={"reason": reason}
                if status == "incomplete" and reason
                else None,
                error={"code": reason, "message": "provider failed"}
                if status == "failed"
                else None,
            ),
        )

    install_azure(monkeypatch, respond)
    supervisor = Supervisor(tmp_path, DirectResponsesAdapter())
    request = TurnRequest.model_validate(turn())
    try:
        await supervisor.submit(request)
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 5)
        events = supervisor.journal.events(request.session_id)
        ended = events[-1]
        assert ended.type == "turn_ended" and ended.data["outcome"] == "errored"
        if status == "incomplete":
            assert ended.data["error"]["kind"] == "model_incomplete"
            expected = "Model response incomplete: " + (reason or "unknown")
        else:
            assert ended.data["error"]["kind"] == "model_failed"
            detail = "server_error: provider failed" if status == "failed" else "unknown"
            expected = f"Model response {status or 'unknown'}: {detail}"
        assert ended.data["error"]["message"] == expected
        starts = [event for event in events if event.type == "model_request_started"]
        usages = [event for event in events if event.type == "usage"]
        assert len(calls) == len(starts) == len(usages) == 1
        assert starts[0].data["request_id"] == usages[0].data["request_id"]
        assert usages[0].data["input_tokens"] == 12 and usages[0].data["output_tokens"] == 8
        assert (
            usages[0].data["cached_input_tokens"] == 3 and usages[0].data["reasoning_tokens"] == 2
        )
        assert not supervisor.journal.pending_requests(request)
        assert not list(tmp_path.rglob("must-not-execute"))
        assert not any(event.type == "tool_use" for event in events)
        if not tool_call and status != "failed":
            assert any(event.type == "agent_message" for event in events)
    finally:
        await supervisor.close()


@pytest.mark.parametrize("tool_name", ["write", "bash"])
@pytest.mark.parametrize("spawned", [False, True])
async def test_cancel_joins_tool_workers_before_terminal_and_session_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str, spawned: bool
):
    def tool_call(count: int) -> list[dict[str, Any]]:
        value = "old" if count == 1 else "new"
        if tool_name == "write":
            arguments = {"path": "victim.txt", "content": value}
        else:
            arguments = {"command": f"sleep 0.1; printf {value} > victim.txt"}
        return [call_item(tool_name, arguments, f"c{count}")]

    script = Script(tool_call(1), tool_call(2), [message_item()])
    install_azure(monkeypatch, script)
    started, release = asyncio.Event(), asyncio.Event()
    create = asyncio.create_subprocess_exec
    children: list[Any] = []
    held = False

    async def delayed_spawn(*args: Any, **kwargs: Any) -> Any:
        nonlocal held
        if held:
            return await create(*args, **kwargs)
        held = True
        if spawned:
            children.append(child := await create(*args, **kwargs))
        started.set()
        await release.wait()
        if not spawned:
            children.append(child := await create(*args, **kwargs))
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    supervisor = Supervisor(tmp_path, DirectResponsesAdapter())
    first = TurnRequest.model_validate(turn())
    second = TurnRequest.model_validate(turn(turn_id="second", key="second"))
    try:
        await supervisor.submit(first)
        await asyncio.wait_for(started.wait(), 5)
        context = supervisor.contexts[first.turn_id]
        assert context.workers
        supervisor.tasks[first.turn_id].cancel()
        await asyncio.sleep(0.05)
        assert not supervisor.journal.ended(first.turn_id)
        with pytest.raises(ConflictError, match="active turn"):
            await supervisor.submit(second)
        supervisor.tasks[first.turn_id].cancel()  # repeated cancellation cannot cut cleanup short
        release.set()
        await asyncio.wait_for(supervisor.tasks[first.turn_id], 5)
        assert not context.workers and not context.process_groups
        assert all(child.returncode is not None for child in children)
        events = supervisor.journal.events(first.session_id)
        assert events[-1].type == "turn_ended" and events[-1].data["outcome"] == "interrupted"
        terminal_seq = events[-1].seq
        await supervisor.submit(second)
        await asyncio.wait_for(supervisor.tasks[second.turn_id], 5)
        assert (context.work / "victim.txt").read_text() == "new"
        late = supervisor.journal.events(first.session_id, terminal_seq)
        assert not any(event.turn_id == first.turn_id for event in late)
        # The interrupted call is only a shell note in the history, never a call to replay.
        resent = script.bodies[1]["input"]
        assert len(script.bodies) == 3
        assert not any(item.get("type") == "function_call" for item in resent)
    finally:
        release.set()
        await supervisor.close()


async def test_invalid_arguments_and_unknown_tools_become_model_visible_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    script = Script(
        [call_item("write", "{not json", "bad-json")],
        [call_item("write", "[1, 2]", "not-object")],
        [call_item("write", {"content": "missing path"}, "missing")],
        [call_item("teleport", {}, "unknown")],
        [message_item()],
    )
    install_azure(monkeypatch, script)
    request = TurnRequest.model_validate(turn())
    request.limits.max_tool_calls = 10
    context, events = recording_context(tmp_path, request)
    await DirectResponsesAdapter().run(context, [])
    sent = [outputs_sent_back(body)[-1]["output"] for body in script.bodies[1:]]
    assert sent[0] == sent[1] == "Invalid tool arguments: expected a JSON object"
    assert sent[2] == '"Tool failed: KeyError"'  # raised inside the tool, as for the SDK loop
    assert sent[3] == "Unknown tool: teleport"
    assert kinds(events).count("tool_use") == 1  # only the call that reached the shell
    assert context.tool_calls == 1


async def test_request_cap_and_shell_tool_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # max_tool_calls is 3, so the loop allows 5 model requests.
    script = Script(*[[call_item("teleport", {}, f"u{i}")] for i in range(9)])
    install_azure(monkeypatch, script)
    context, _ = recording_context(tmp_path / "unknown")
    with pytest.raises(HarnessError) as capped:
        await DirectResponsesAdapter().run(context, [])
    assert capped.value.kind == "max_turns" and len(script.bodies) == 5

    script = Script(
        *[[call_item("write", {"path": "f", "content": str(i)}, f"w{i}")] for i in range(9)]
    )
    install_azure(monkeypatch, script)
    context, events = recording_context(tmp_path / "writes")
    with pytest.raises(HarnessError) as limited:
        await DirectResponsesAdapter().run(context, [])
    assert limited.value.kind == "tool_limit" and len(script.bodies) == 4
    last = [data for kind, data in events if kind == "tool_result"][-1]
    assert last["output"] == "Maximum tool calls exceeded" and last["is_error"]


# --- MCP over streamable HTTP -------------------------------------------------------------------


async def test_mcp_tools_bind_credentials_confirm_and_redact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async with mcp_server(tmp_path, monkeypatch, leak_secret=True) as mcp:
        monkeypatch.setenv("HARNESS_MCP_CRED_SAMPLE", SECRET)
        binding = {"name": "HARNESS_MCP_CRED_SAMPLE", "origin": mcp.origin, "tenants": ["*"]}
        monkeypatch.setenv("HARNESS_MCP_CRED_BINDINGS", json.dumps([binding]))
        request = TurnRequest.model_validate(turn())
        request.tool_policy = {"sample/*": "deny", "sample/echo": "ask"}
        request.mcp_servers = [
            MCPServer(name="sample", url=mcp.url, headers_env={"Authorization": binding["name"]})
        ]
        script = Script(
            [call_item("sample__echo", {"text": "mcp-result"}, "mcp-call")], [message_item()]
        )
        install_azure(monkeypatch, script)
        events: Events = []

        async def emit(kind: str, data: dict[str, Any]) -> None:
            events.append((kind, data))
            if kind == "requires_action":
                assert mcp.executed == []  # nothing ran before the decision
                action = str(data["action_id"])
                context.pending[action].set_result(
                    ActionRequest(action_id=action, decision="allow")
                )

        context = TurnContext(request, tmp_path, emit, credentials=turn_credentials(request, None))
        history = await DirectResponsesAdapter().run(context, [])
    assert mcp.executed == ["mcp-result"]
    assert any(headers.get("authorization") == SECRET for headers in mcp.headers)
    tools = {tool["name"]: tool for tool in script.bodies[0]["tools"]}
    assert set(tools) >= {"sample__echo", "write"} and "sample__forbidden" not in tools
    assert tools["sample__echo"]["strict"] is False and tools["write"]["strict"] is True
    use = next(data for kind, data in events if kind == "tool_use")
    assert use == {
        "call_id": "mcp-call",
        "name": "sample/echo",
        "input": {"text": "mcp-result"},
        "executor": "mcp",
    }
    assert kinds(events).count("requires_action") == 1 and kinds(events).count("usage") == 2
    assert "[redacted]" in str(script.bodies[1]["input"])
    assert "synthetic-operator-mcp-secret" not in str(script.bodies) + str(history)


@pytest.mark.parametrize("failure", ["model_failed", "tool_limit"])
async def test_harness_errors_keep_their_kind_with_an_mcp_server_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    async with mcp_server(tmp_path, monkeypatch) as mcp:
        request = TurnRequest.model_validate(turn())
        request.mcp_servers = [MCPServer(name="sample", url=mcp.url)]
        request.limits.max_tool_calls = 0
        if failure == "model_failed":
            outputs = [response_json([], status="failed", error={"code": "x", "message": "y"})]
        else:
            outputs = [response_json([call_item("sample__echo", {"text": "t"})])]
        install_azure(monkeypatch, lambda _: httpx.Response(200, json=outputs[0]))
        context, _ = recording_context(tmp_path, request)
        with pytest.raises(HarnessError) as caught:  # not an ExceptionGroup from the transport
            await DirectResponsesAdapter().run(context, [])
        assert caught.value.kind == failure and mcp.executed == []


async def test_cancel_during_an_mcp_tool_call_unwinds_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async with mcp_server(tmp_path, monkeypatch) as mcp:
        request = TurnRequest.model_validate(turn())
        request.mcp_servers = [MCPServer(name="sample", url=mcp.url)]
        install_azure(monkeypatch, Script([call_item("sample__slow", {})]))
        context, events = recording_context(tmp_path, request)
        task = asyncio.create_task(DirectResponsesAdapter().run(context, []))
        started = asyncio.create_task(mcp.slow_started.wait())
        await asyncio.wait({task, started}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert not task.done(), task.exception()  # the turn must still be inside the tool
        assert started.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        await context.stop_workers()
    result = next(data for kind, data in events if kind == "tool_result")
    assert result["is_error"] and "interrupted" in result["output"]


# --- Through the real service: mux driver over loopback uvicorn ---------------------------------


@asynccontextmanager
async def direct_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: Any, **session: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, Any, Any, Path]]:
    """A real service and mux driver whose model is a mocked Azure endpoint (Script or handler)."""
    install_azure(monkeypatch, model)
    async with serve_harness(tmp_path / "harness-home", serving_adapter(monkeypatch)) as running:
        client, _, home = running
        driver, created = await driver_session(client, **session)
        if isinstance(model, Script):
            model.before = partial(journaled_before_io, client, created.ref.id)
        try:
            yield client, driver, created, home
        finally:
            await driver.aclose()


async def journaled_before_io(client: httpx.AsyncClient, session_id: str, count: int, _: Any):
    """At each model request the durable log holds exactly one new start and all earlier usage."""
    events = (await client.get(f"/v0/sessions/{session_id}/events")).json()["events"]
    assert sum(event["type"] == "model_request_started" for event in events) == count
    assert sum(event["type"] == "usage" for event in events) == count - 1


ASK_WRITE = [
    {"kind": "builtin", "name": name, "permission": "ask" if name == "write" else "auto"}
    for name in ("bash", "read", "write", "edit")
]


async def requires_action(driver: Any, session: Any) -> Any:
    async with asyncio.timeout(10):
        while True:
            snapshot = await driver.events.reconcile(SCOPE, session.ref)
            if snapshot.state == "requires_action":
                return snapshot.required_actions[0]
            assert snapshot.state != "idle", "the turn ended before asking"
            await asyncio.sleep(0.01)


async def test_interop_tool_write_is_metered_and_journaled_before_each_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    script = Script(
        [call_item("write", {"path": "result.txt", "content": "durable"})], [message_item("saved")]
    )
    async with direct_harness(tmp_path, monkeypatch, script) as (client, driver, session, home):
        await driver.events.send(SCOPE, session.ref, [message("tool")], key="send")
        events = await collect(driver, session)
        started = "native.selfhosted.model_request_started"
        assert [event.type for event in events] == [
            "session.status_running",
            started,
            "usage.observed",
            "agent.tool_use",
            "agent.tool_result",
            started,
            "usage.observed",
            "agent.message.delta",
            "agent.message",
            "session.turn_ended",
        ]
        assert events[-1].payload["outcome"] == "completed"
        assert (workspace(home, session.ref.id) / "result.txt").read_text() == "durable"
        usage = await driver.usage.reconcile(SCOPE, session.ref)
        assert [(u.completeness, u.input_tokens, u.input_cached_tokens) for u in usage] == [
            ("measured", 12, 3)
        ] * 2
        delta = next(e for e in events if e.type == "agent.message.delta")
        final = next(e for e in events if e.type == "agent.message")
        assert delta.payload["item_id"] == final.payload["item_id"]
        # A resend with the same key replays the journal and never reaches the model again.
        await driver.events.send(SCOPE, session.ref, [message("tool")], key="send")
        assert len(script.bodies) == 2


@pytest.mark.parametrize("decision", ["allow", "deny", "result"])
async def test_interop_ask_decision_allow_deny_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str
):
    call = call_item("write", {"path": "result.txt", "content": "durable"}, "ask-call")
    script = Script([call], [message_item()])
    async with direct_harness(tmp_path, monkeypatch, script, tools=ASK_WRITE) as running:
        client, driver, session, home = running
        sent = await driver.events.send(SCOPE, session.ref, [message("ask")], key="send")
        action = await requires_action(driver, session)
        assert action.payload["name"] == "write" and len(script.bodies) == 1
        assert not (workspace(home, session.ref.id) / "result.txt").exists()
        answers = {
            "allow": UserToolConfirmation(action_id=action.id, decision="allow"),
            "deny": UserToolConfirmation(action_id=action.id, decision="deny"),
            "result": UserToolResult(action_id=action.id, content=(TextPart(text="host-result"),)),
        }
        await driver.events.send(
            SCOPE, session.ref, [answers[decision]], key="decide", expected_turn=sent.turn_id
        )
        events = await collect(driver, session, after="0")
        assert events[-1].payload["outcome"] == "completed"
        written = workspace(home, session.ref.id) / "result.txt"
        result = next(e for e in events if e.type == "agent.tool_result")
        sent_back = outputs_sent_back(script.bodies[1])
        assert [item["call_id"] for item in sent_back] == ["ask-call"]  # paired with its call
        if decision == "allow":
            assert written.read_text() == "durable" and not result.payload["is_error"]
        elif decision == "deny":
            assert not written.exists() and result.payload["is_error"]
            assert "denied" in str(result.payload["content"]).lower()
        else:
            assert not written.exists() and "host-result" in str(result.payload["content"])
            assert "host-result" in sent_back[0]["output"]


async def test_interop_cancel_mid_request_leaves_unknown_usage_and_a_usable_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from datetime import UTC, datetime, timedelta

    hanging, script = asyncio.Event(), Script([message_item("after")])

    async def respond(request: httpx.Request) -> httpx.Response:
        if not hanging.is_set():
            hanging.set()
            await asyncio.Event().wait()  # the first model request never returns
        return await script(request)

    async with direct_harness(tmp_path, monkeypatch, respond) as (client, driver, session, _):
        sent = await driver.events.send(SCOPE, session.ref, [message("wait")], key="send")
        await asyncio.wait_for(hanging.wait(), 10)
        cancelled = await driver.events.cancel(SCOPE, session.ref, turn_id=sent.turn_id, key="c")
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        stopped = await driver.events.wait_stopped(SCOPE, cancelled, deadline=deadline)
        assert stopped.stopped and stopped.outcome == "interrupted"
        durable = (await client.get(f"/v0/sessions/{session.ref.id}/events")).json()["events"]
        start = next(e for e in durable if e["type"] == "model_request_started")
        unknown = next(e for e in durable if e["type"] == "error")
        assert unknown["data"]["kind"] == "usage_unavailable"
        assert unknown["data"]["request_id"] == start["data"]["request_id"]
        assert not any(e["type"] == "usage" for e in durable)
        usage = await driver.usage.reconcile(SCOPE, session.ref)
        assert [u.completeness for u in usage] == ["unknown"]
        again = await driver.events.cancel(SCOPE, session.ref, turn_id=sent.turn_id, key="again")
        assert again.status == "already_stopped"
        await driver.events.send(SCOPE, session.ref, [message("next")], key="next")
        assert (await collect(driver, session))[-1].payload["outcome"] == "completed"
        assert len(script.bodies) == 1


async def test_interop_mcp_exposure_approval_and_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async with mcp_server(tmp_path, monkeypatch) as mcp:
        # One response asks for the approved tool and for a tool the policy hides.
        script = Script(
            [
                call_item("sample__echo", {"text": "mcp-result"}, "mcp-call"),
                call_item("sample__forbidden", {}, "denied-call"),
            ],
            [message_item()],
        )
        servers = [
            {
                "name": "sample",
                "url": mcp.url,
                "tool_policy": {"allowed_tools": ["echo"], "echo": "ask"},
            }
        ]
        async with direct_harness(tmp_path, monkeypatch, script, mcp_servers=servers) as running:
            _, driver, session, _ = running
            sent = await driver.events.send(SCOPE, session.ref, [message("mcp")], key="send")
            action = await requires_action(driver, session)
            assert mcp.executed == [] and action.payload["name"] == "sample/echo"
            exposed = [tool["name"] for tool in script.bodies[0]["tools"]]
            assert "sample__echo" in exposed and "sample__forbidden" not in exposed
            answer = UserToolConfirmation(action_id=action.id, decision="allow")
            await driver.events.send(
                SCOPE, session.ref, [answer], key="approve", expected_turn=sent.turn_id
            )
            recorded = await collect(driver, session, after="0")
            assert recorded[-1].payload["outcome"] == "completed"
            denied = [
                e
                for e in recorded
                if e.type == "agent.tool_result" and e.payload["call_id"] == "denied-call"
            ]
            assert len(denied) == 1 and denied[0].payload["is_error"]
            assert "denied" in str(denied[0].payload["content"]).lower()
            assert mcp.executed == ["mcp-result"]
            usage = await driver.usage.reconcile(SCOPE, session.ref)
            assert [u.completeness for u in usage] == ["measured", "measured"]
            sent_back = {item["call_id"] for item in outputs_sent_back(script.bodies[1])}
            assert sent_back == {"mcp-call", "denied-call"}


# --- Phase 2: the same loop over Anthropic Messages --------------------------------------------


def thinking_block(signature: str = "sig-1") -> dict[str, Any]:
    return {"type": "thinking", "thinking": "plan the write", "signature": signature}


def tool_use_block(name: str, tool_input: dict[str, Any], tool_id: str = "toolu_1"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def text_block(text: str = "done") -> dict[str, Any]:
    return {"type": "text", "text": text}


async def test_anthropic_tool_loop_meters_cache_tokens_and_keeps_thinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    write = tool_use_block("write", {"path": "from-claude.txt", "content": "claude"})
    first_usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 20,
        "output_tokens": 5,
    }

    async def before(count: int, body: dict[str, Any]) -> None:
        assert body["system"] == "Help\n\n" and body["max_tokens"] > 0
        assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
        assert body["thinking"] == {"type": "adaptive"}
        assert body["cache_control"] == {"type": "ephemeral"}
        assert [tool["name"] for tool in body["tools"]] == ["bash", "read", "write", "edit"]
        assert body["tools"][2]["input_schema"]["required"] == ["path", "content"]
        assert body["messages"][0] == {"role": "user", "content": [text_block("look")]}
        if count == 2:
            assistant, results = body["messages"][1:]
            assert assistant["role"] == "assistant"
            assert assistant["content"] == [thinking_block(), write]  # signed, unmodified
            assert results["role"] == "user"
            assert results["content"][0]["tool_use_id"] == "toolu_1"
            assert results["content"][0]["type"] == "tool_result"

    script = anthropic_script(
        message_json([thinking_block(), write], stop_reason="tool_use", usage=first_usage),
        message_json([text_block("saved")], usage={"input_tokens": 3, "output_tokens": 2}),
        before=before,
    )
    install_anthropic(monkeypatch, script)
    request = TurnRequest.model_validate(turn() | {"input": [TEXT]})
    context, events = recording_context(tmp_path, request)
    history = await DirectAnthropicAdapter().run(context, [])
    assert (context.work / "from-claude.txt").read_text() == "claude"
    usages = [data for kind, data in events if kind == "usage"]
    assert [(u["input_tokens"], u["cached_input_tokens"], u["output_tokens"]) for u in usages] == [
        (130, 100, 5),  # input includes cache reads and writes, as in the journal's contract
        (3, 0, 2),
    ]
    starts = [data["request_id"] for kind, data in events if kind == "model_request_started"]
    assert starts == [u["request_id"] for u in usages]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert kinds(events).count("tool_use") == kinds(events).count("tool_result") == 1
    assert next(d for k, d in events if k == "agent_message")["parts"][0]["text"] == "saved"


@pytest.mark.parametrize(
    "stop_reason,kind,text",
    [
        ("max_tokens", "model_incomplete", "Model response incomplete: max_tokens"),
        ("refusal", "model_failed", "Model response refusal: unknown"),
    ],
)
async def test_anthropic_unfinished_response_keeps_usage_and_never_runs_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_reason: str, kind: str, text: str
):
    write = tool_use_block("write", {"path": "must-not-execute", "content": "bad"})
    script = anthropic_script(message_json([write], stop_reason=stop_reason))
    install_anthropic(monkeypatch, script)
    context, events = recording_context(tmp_path)
    with pytest.raises(HarnessError, match=text) as caught:
        await DirectAnthropicAdapter().run(context, [])
    assert caught.value.kind == kind
    assert kinds(events).count("usage") == 1 and "tool_use" not in kinds(events)
    assert not list(tmp_path.rglob("must-not-execute"))


async def test_anthropic_overflow_retries_once_with_compacted_recent_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bash = tool_use_block("bash", {"command": "head -c 20000 /dev/zero | tr '\\0' x"})
    long_prompt = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "prompt is too long: 250000 > 200000",
        },
    }
    replies = [
        httpx.Response(200, json=message_json([bash], stop_reason="tool_use")),
        httpx.Response(400, json=long_prompt),
        httpx.Response(200, json=message_json([text_block("ok")])),
    ]
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return replies[len(bodies) - 1]

    install_anthropic(monkeypatch, respond)
    context, events = recording_context(tmp_path)
    history = await DirectAnthropicAdapter().run(
        context, [{"role": "user", "content": "old-history-marker"}]
    )
    assert len(bodies) == 3 and "old-history-marker" not in str(bodies[2]) + str(history)
    assert "old-history-marker" in str(bodies[1])
    retained = bodies[2]["messages"]
    assert [m["role"] for m in retained] == ["user", "assistant", "user"]
    result = retained[2]["content"][0]["content"]
    assert result.endswith("[output truncated for context]") and len(result) < 9000
    assert [k for k, d in events if k == "error" and d["kind"] == "context_truncated"]
    assert kinds(events).count("model_request_started") == 3


async def test_anthropic_server_error_is_unmeasured_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            529,
            json={
                "type": "error",
                "error": {"type": "overloaded_error", "message": "private-error"},
            },
        )

    install_anthropic(monkeypatch, respond)
    context, events = recording_context(tmp_path)
    with pytest.raises(HarnessError) as caught:
        await DirectAnthropicAdapter().run(context, [])
    assert caught.value.kind == "usage_unavailable" and len(calls) == 1
    assert "usage" not in kinds(events) and "private-error" not in str(events)


# --- Credential redaction, probes and shell coupling ported from the SDK adapter's tests --------


async def test_mcp_initialization_failure_logs_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    import logging

    from daimon_harness.credentials import VIRTUAL_ENV
    from daimon_harness.runtime import discard_event
    from pydantic import ValidationError

    secret = "Bearer synthetic-mcp-initialization-secret"
    monkeypatch.setenv("HARNESS_DAIMON_MCP_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
    request = TurnRequest.model_validate(turn())
    request.mcp_servers = [
        MCPServer(
            name="daimon-mcp",
            url="https://mcp.example/mcp",
            headers_env={"Authorization": VIRTUAL_ENV},
        )
    ]
    context = TurnContext(request, tmp_path, discard_event, credentials={VIRTUAL_ENV: secret})

    def invalid_response(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == secret
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {"echo": secret}})

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(invalid_response), **kwargs)

    monkeypatch.setattr(direct_engine, "bound_mcp_http_client", client_factory)
    with caplog.at_level(logging.WARNING), pytest.raises(ValidationError):
        await DirectResponsesAdapter().run(context, [])
    assert "synthetic-mcp-initialization-secret" not in caplog.text


@pytest.mark.parametrize("auth_mode", ["key", "bearer"])
async def test_debug_logs_keep_header_filters_and_hide_all_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    auth_mode: str,
):
    import logging

    key, bearer = "synthetic-azure-debug-key-67241", "synthetic-azure-debug-bearer-67241"
    harness, operator = "synthetic-harness-debug-token-67241", "synthetic-unused-mcp-secret-67241"
    client_secret = "synthetic-identity-debug-secret-67241"
    monkeypatch.setenv("HARNESS_TOKEN", harness)
    monkeypatch.setenv("HARNESS_MCP_CRED_UNUSED", operator)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", client_secret)

    async def token_provider() -> str:
        return bearer

    monkeypatch.setattr(direct_engine, "get_bearer_token_provider", lambda *args: token_provider)
    filtered_headers: list[dict[str, str]] = []

    class AfterSDKFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if isinstance(record.args, dict) and "headers" in record.args:
                filtered_headers.append(record.args["headers"].copy())
            return True

    probe, sdk_logger = AfterSDKFilter(), logging.getLogger("openai._base_client")
    sdk_logger.addFilter(probe)
    header_name = "api-key" if auth_mode == "key" else "Authorization"
    sent = key if auth_mode == "key" else f"Bearer {bearer}"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers[header_name] == sent
        echoed = [harness, operator, client_secret, key if auth_mode == "key" else bearer]
        logging.getLogger("sdk.echo").warning("credentials: %s", echoed)
        return httpx.Response(200, json=response_json([message_item()]))

    install_azure(monkeypatch, respond)
    if auth_mode == "key":
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", key)
    else:
        monkeypatch.delenv("AZURE_OPENAI_API_KEY")
        monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1:1234/token")
        monkeypatch.setenv("IDENTITY_HEADER", "synthetic-identity-header")
    adapter = DirectResponsesAdapter()
    try:
        with caplog.at_level(logging.DEBUG):
            await adapter.run(recording_context(tmp_path)[0], [])
    finally:
        sdk_logger.removeFilter(probe)
        if adapter.azure.credential is not None:
            await adapter.azure.credential.close()
    assert "Request options:" in caplog.text and filtered_headers
    assert any(headers.get(header_name) == "<redacted>" for headers in filtered_headers)
    for secret in (key, bearer, harness, operator, client_secret):
        assert secret not in caplog.text
    assert "[redacted]" in caplog.text


@pytest.mark.parametrize("echo", ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-luna-2026-09-22"])
async def test_c12_checked_client_on_loopback_without_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, echo: str
):
    from .live_probe import C12ModelMismatch

    calls: list[dict[str, Any]] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            length = next(
                int(line.split(":", 1)[1])
                for line in header.split("\r\n")
                if line.lower().startswith("content-length:")
            )
            calls.append(json.loads(await reader.readexactly(length)))
            payload = json.dumps(response_json([message_item("ready")], model=echo)).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "local-test-placeholder")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-6-luna")
        monkeypatch.setattr(direct_engine, "AsyncAzureOpenAI", checked_client)
        context, events = recording_context(tmp_path)
        async with asyncio.timeout(15):
            if echo == "gpt-6.1-sol":
                with pytest.raises(C12ModelMismatch, match="C12: Responses model") as error:
                    await run_luna_probe(DirectResponsesAdapter(), context)  # type: ignore[arg-type]
                assert error.value.kind == "c12_model_mismatch" and not error.value.retryable
                assert not any(kind == "agent_message" for kind in kinds(events))
            else:
                await run_luna_probe(DirectResponsesAdapter(), context)  # type: ignore[arg-type]
                assert kinds(events).count("usage") == kinds(events).count("agent_message") == 1
                assert "error" not in kinds(events)
    assert len(calls) == 1 and calls[0]["model"] == "gpt-6-luna"  # nothing retried


def test_managed_identity_endpoint_without_secret_header_is_refused(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1/token")
    with pytest.raises(RuntimeError, match="header-protected"):
        DirectResponsesAdapter()


async def test_interop_narrowed_agent_omits_and_refuses_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    script = Script([call_item("write", {"path": "result.txt", "content": "x"})], [message_item()])
    only_read = [{"kind": "builtin", "name": "read", "permission": "auto"}]
    async with direct_harness(tmp_path, monkeypatch, script, tools=only_read) as running:
        _, driver, session, home = running
        await driver.events.send(SCOPE, session.ref, [message("tool")], key="send")
        events = await collect(driver, session, after="0")
        assert [tool["name"] for tool in script.bodies[0]["tools"]] == ["read"]
        denied = next(event for event in events if event.type == "agent.tool_result")
        assert denied.payload["is_error"] and "denied" in str(denied.payload["content"]).lower()
        assert not (workspace(home, session.ref.id) / "result.txt").exists()
        assert events[-1].payload["outcome"] == "completed"
