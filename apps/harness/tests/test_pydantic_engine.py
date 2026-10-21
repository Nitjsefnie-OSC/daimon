"""The Pydantic AI engine against the same mocked Azure HTTP as the Agents SDK engine.

Skipped unless ``pydantic-ai-slim`` is installed (it is not in the locked repo environment).
Ported from ``test_model_adapter.py`` and ``test_interop_driver.py`` where the test exercises
the agent loop; the rest of those files tests the shell and is engine independent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("pydantic_ai")

from daimon_harness import model_adapter, pydantic_engine
from daimon_harness.credentials import turn_credentials
from daimon_harness.errors import HarnessError
from daimon_harness.journal import ConflictError, Journal
from daimon_harness.pydantic_engine import PydanticAIAdapter
from daimon_harness.runtime import TurnContext
from daimon_harness.service import Supervisor
from mux.contracts.actions import UserToolConfirmation, UserToolResult
from mux.contracts.events import TextPart
from mux.drivers.selfhosted.wire import ActionRequest, MCPServer, TurnRequest
from openai import AsyncAzureOpenAI

from .conftest import serve_harness, turn
from .interop_support import SCOPE, collect, driver_session, message, workspace
from .live_probe import C12ModelMismatch, checked_client, luna_deployment, run_luna_probe
from .pydantic_support import (
    Azure,
    EngineProxy,
    call,
    install,
    loopback_azure,
    mcp_server,
    reasoning,
    reply,
    text,
)

Events = list[tuple[str, dict[str, Any]]]


def collector() -> tuple[Events, Any]:
    events: Events = []

    async def emit(kind: str, data: dict[str, Any]) -> None:
        events.append((kind, data))

    return events, emit


def kinds(events: Events, kind: str) -> list[dict[str, Any]]:
    return [data for event, data in events if event == kind]


def context_for(tmp_path: Path, emit: Any, **changes: object) -> TurnContext:
    request = TurnRequest.model_validate(turn()).model_copy(update=changes)
    return TurnContext(request, tmp_path, emit)


# --- Ported: metering --------------------------------------------------------------------


async def test_managed_identity_client_uses_real_async_transport_and_redacts_the_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ported from the Agents SDK test: the credential comes from the shared base class and
    the bearer token reaches Azure, and the logs, through this engine's own client."""
    from .identity_probe import TOKEN, managed_identity_endpoint

    seen: list[str] = []

    def azure(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        logging.getLogger("pydantic_engine_test").warning(
            "sent %s", request.headers["authorization"]
        )
        return reply([text("ok")])

    async with managed_identity_endpoint() as calls:
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        adapter = PydanticAIAdapter()
        assert adapter.key is None and adapter.credential is not None
        install(monkeypatch, azure)
        monkeypatch.delenv("AZURE_OPENAI_API_KEY")
        _, emit = collector()
        try:
            await adapter.run(context_for(tmp_path, emit), [])
        finally:
            await adapter.credential.close()
        assert seen == ["Bearer " + TOKEN] and len(calls) == 1
    logged = [
        record.getMessage() for record in caplog.records if record.name == "pydantic_engine_test"
    ]
    assert logged and TOKEN not in str(logged) and "[redacted]" in str(logged)


async def test_tool_loop_meters_each_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = Journal(tmp_path)
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    azure = Azure(
        [
            [
                reasoning("rs_1", "encrypted-reasoning"),
                call("write", {"path": "from-sdk.txt", "content": "sdk"}, "call-1"),
            ]
        ]
    )

    def respond(http: httpx.Request) -> httpx.Response:
        committed = journal.events("session-1")
        # The start is durable before the request leaves the process.
        assert committed[-1].type == "model_request_started"
        assert (
            sum(event.type == "model_request_started" for event in committed)
            == len(azure.bodies) + 1
        )
        return azure(http)

    install(monkeypatch, respond)
    events, collect_event = collector()

    async def emit(kind: str, data: dict[str, Any]) -> None:
        journal.append(request, kind, data)
        await collect_event(kind, data)

    context = TurnContext(request, tmp_path, emit)
    history = await PydanticAIAdapter().run(context, [])
    first = azure.bodies[0]
    assert first["store"] is False and first["parallel_tool_calls"] is False
    assert first["include"] == ["reasoning.encrypted_content"]
    reasoning_item = next(i for i in azure.bodies[1]["input"] if i.get("type") == "reasoning")  # type: ignore[union-attr]
    assert reasoning_item["encrypted_content"] == "encrypted-reasoning"
    assert (tmp_path / "work/from-sdk.txt").read_text() == "sdk"
    usages, starts = kinds(events, "usage"), kinds(events, "model_request_started")
    assert [s["request_id"] for s in starts] == [u["request_id"] for u in usages]
    assert len(usages) == 2 and all(
        u
        == {
            "request_id": u["request_id"],
            "model": "gpt-6-luna",
            "input_tokens": 12,
            "cached_input_tokens": 3,
            "output_tokens": 8,
            "reasoning_tokens": 2,
        }
        for u in usages
    )
    assert journal.pending_requests(request) == {}
    journal.close()
    assert len(history) == 4  # request, reasoning+call, result, assistant
    assert len(kinds(events, "tool_use")) == len(kinds(events, "tool_result")) == 1
    deltas, finals = kinds(events, "agent_message_delta"), kinds(events, "agent_message")
    assert [d["message_id"] for d in deltas] == [f["message_id"] for f in finals] and finals


@pytest.mark.skipif(
    not (
        os.environ.get("AZURE_OPENAI_ENDPOINT")
        and (os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("AZURE_CLIENT_ID"))
    ),
    reason="requires explicit Azure OpenAI credentials",
)
async def test_live_azure_responses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = turn()
    body["model"] = luna_deployment()
    body["input"] = [{"type": "text", "text": "Reply with the word ready. Do not use tools."}]
    monkeypatch.setattr(pydantic_engine, "AsyncAzureOpenAI", checked_client)
    events, emit = collector()
    context = TurnContext(TurnRequest.model_validate(body), tmp_path, emit)
    await run_luna_probe(PydanticAIAdapter(), context)  # type: ignore[arg-type]
    assert len(kinds(events, "usage")) == 1 and kinds(events, "agent_message")


@pytest.mark.parametrize("echo", ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-luna-2026-09-22"])
async def test_c12_checked_client_through_the_engine_on_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, echo: str
) -> None:
    """Ported from ``test_live_probe``: a hook error raised inside the HTTP client must come out
    of this engine's exception chain intact, and nothing may retry."""
    calls: list[dict[str, object]] = []
    errors: list[str] = []
    async with loopback_azure(echo, calls, errors) as endpoint:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "local-test-placeholder")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-6-luna")
        monkeypatch.setattr(pydantic_engine, "AsyncAzureOpenAI", checked_client)
        events, emit = collector()
        context = context_for(tmp_path, emit)
        async with asyncio.timeout(15):
            if echo == "gpt-6.1-sol":
                with pytest.raises(C12ModelMismatch, match="C12: Responses model") as error:
                    await run_luna_probe(PydanticAIAdapter(), context)  # type: ignore[arg-type]
                assert error.value.kind == "c12_model_mismatch" and not error.value.retryable
                assert [d["kind"] for d in kinds(events, "error")].count("c12_model_mismatch") == 1
                assert not kinds(events, "agent_message")
            else:
                await run_luna_probe(PydanticAIAdapter(), context)  # type: ignore[arg-type]
                assert len(kinds(events, "usage")) == len(kinds(events, "agent_message")) == 1
    assert errors == [] and len(calls) == 1 and calls[0]["model"] == "gpt-6-luna"


@pytest.mark.parametrize("failure", ["http", "missing_usage", "cancelled"])
async def test_unmeasured_request_never_invents_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    started = asyncio.Event()
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        if failure == "cancelled":
            await asyncio.Event().wait()
        if failure == "http":
            return httpx.Response(
                500,
                headers={"x-request-id": "failed-request"},
                json={"error": {"message": "private-error", "type": "server_error"}},
            )
        return reply([text("done")], usage=None)

    install(monkeypatch, respond)
    events, emit = collector()
    task = asyncio.create_task(PydanticAIAdapter().run(context_for(tmp_path, emit), []))
    await asyncio.wait_for(started.wait(), 5)
    if failure == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(HarnessError) as raised:
            await task
        assert raised.value.kind == ("usage_unavailable" if failure == "http" else "usage_missing")
    assert calls == 1  # no retry at any layer
    assert not kinds(events, "usage")
    error = kinds(events, "error")[0]
    assert error["kind"] == "usage_unavailable"
    assert kinds(events, "model_request_started")[0]["request_id"] == error["request_id"]
    assert "private-error" not in str(events)


@pytest.mark.parametrize("after_tool", [False, True])
async def test_context_overflow_truncates_history_and_inventories_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_tool: bool
) -> None:
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        count = len(bodies)
        if after_tool and count == 1:
            return reply(
                [
                    reasoning("reasoning-for-write", "opaque-required-reasoning"),
                    call("write", {"path": "once.txt", "content": "once"}, "write"),
                ]
            )
        if count == 1 + int(after_tool):
            assert "old-history-marker" in str(bodies[-1]["input"])
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "too long",
                        "code": "context_length_exceeded",
                        "type": "invalid_request_error",
                    }
                },
            )
        retained = bodies[-1]["input"]
        assert "old-history-marker" not in str(retained)
        if after_tool:
            thought = next(i for i in retained if i.get("type") == "reasoning")
            assert thought["id"] == "reasoning-for-write"
            assert thought["encrypted_content"] == "opaque-required-reasoning"
            tool_call = next(i for i in retained if i.get("type") == "function_call")
            result = next(i for i in retained if i.get("type") == "function_call_output")
            assert tool_call["call_id"] == result["call_id"] == "write"
            assert retained.index(thought) < retained.index(tool_call) < retained.index(result)
        return reply([text("ok")])

    install(monkeypatch, respond)
    events, emit = collector()
    runtime = context_for(tmp_path, emit)
    old = [{"role": "user", "content": "old-history-marker"}]
    history = await PydanticAIAdapter().run(runtime, old)  # type: ignore[arg-type]
    assert len(bodies) == 2 + int(after_tool)
    if after_tool:
        assert runtime.tool_calls == 1
        assert "function_call_output" in str(bodies[-1]["input"])
        assert (runtime.work / "once.txt").read_text() == "once"
    assert "old-history-marker" not in str(history)
    starts = [d["request_id"] for d in kinds(events, "model_request_started")]
    unknown = [d["request_id"] for d in kinds(events, "error") if d["kind"] == "usage_unavailable"]
    measured = [d["request_id"] for d in kinds(events, "usage")]
    assert len(starts) == 2 + int(after_tool) and len(set(starts)) == len(starts)
    assert set(starts) == set(unknown + measured)
    assert any(d["kind"] == "context_truncated" for d in kinds(events, "error"))


async def test_denied_call_retains_reasoning_and_paired_output_in_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    azure = Azure(
        [
            [
                reasoning("reasoning-1", "opaque-reasoning"),
                call("bash", {"command": "touch forbidden"}, "denied-call"),
            ]
        ]
    )
    install(monkeypatch, azure)
    request = TurnRequest.model_validate(turn())
    request.tool_policy = {"bash": "deny"}
    events, emit = collector()
    runtime = TurnContext(request, tmp_path, emit)
    history = await PydanticAIAdapter().run(runtime, [])
    second = TurnContext(request.model_copy(update={"turn_id": "second"}), tmp_path, emit)
    await PydanticAIAdapter().run(second, history)
    assert len(azure.bodies) == 3
    assert all("bash" not in azure.tool_names(i) for i in range(3))
    for index in (1, 2):
        items = azure.bodies[index]["input"]
        calls = [i for i in items if i.get("type") == "function_call"]
        results = [i for i in items if i.get("type") == "function_call_output"]
        assert len(calls) == len(results) == 1
        assert calls[0]["call_id"] == results[0]["call_id"] == "denied-call"
        assert "denied by policy" in results[0]["output"]
        assert any(i.get("type") == "reasoning" for i in items)
    assert not (runtime.work / "forbidden").exists()
    use = kinds(events, "tool_use")[0]
    assert use["name"] == "bash" and use["input"] == {"arguments": '{"command": "touch forbidden"}'}
    result = kinds(events, "tool_result")[0]
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
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        output = (
            [
                call("write", {"path": "must-not-execute", "content": "bad"}, "unsafe")
                | {"status": "incomplete"}
            ]
            if tool_call
            else [text("partial", "msg") | {"status": "incomplete"}]
        )
        return reply(
            [] if status == "failed" and not tool_call else output,
            response_id="truncated-response",
            status=status,
            incomplete_details={"reason": reason} if status == "incomplete" and reason else None,
            error={"code": reason, "message": "provider failed"} if status == "failed" else None,
        )

    install(monkeypatch, respond)
    supervisor = Supervisor(tmp_path, PydanticAIAdapter())
    request = TurnRequest.model_validate(turn())
    try:
        await supervisor.submit(request)
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 5)
        events = supervisor.journal.events(request.session_id)
        ended = events[-1]
        assert ended.type == "turn_ended" and ended.data["outcome"] == "errored"
        if status == "incomplete":
            assert ended.data["error"]["kind"] == "model_incomplete"  # type: ignore[index]
            assert ended.data["error"]["message"] == "Model response incomplete: " + (
                reason or "unknown"
            )  # type: ignore[index]
        else:
            provider_error = "server_error: provider failed" if status == "failed" else "unknown"
            assert ended.data["error"]["kind"] == "model_failed"  # type: ignore[index]
            assert (
                ended.data["error"]["message"]
                == f"Model response {status or 'unknown'}: {provider_error}"
            )  # type: ignore[index]
        starts = [e for e in events if e.type == "model_request_started"]
        usages = [e for e in events if e.type == "usage"]
        assert len(calls) == len(starts) == len(usages) == 1
        assert starts[0].data["request_id"] == usages[0].data["request_id"]
        assert usages[0].data["input_tokens"] == 12 and usages[0].data["output_tokens"] == 8
        assert (
            usages[0].data["cached_input_tokens"] == 3 and usages[0].data["reasoning_tokens"] == 2
        )
        assert not supervisor.journal.pending_requests(request)
        assert not list(tmp_path.rglob("must-not-execute"))
        assert not any(e.type == "tool_use" for e in events)
        if not tool_call and status != "failed":
            assert any(e.type == "agent_message" for e in events)
    finally:
        await supervisor.close()


@pytest.mark.parametrize("tool_name", ["write", "bash"])
@pytest.mark.parametrize("spawned", [False, True])
async def test_cancel_joins_tool_workers_before_terminal_and_session_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str, spawned: bool
) -> None:
    calls: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        value = "old" if len(calls) == 1 else "new"
        arguments = (
            {"path": "victim.txt", "content": value}
            if tool_name == "write"
            else {"command": f"sleep 0.1; printf {value} > victim.txt"}
        )
        output = (
            [call(tool_name, arguments, f"c{len(calls)}")]
            if len(calls) < 3
            else [text("done", "done")]
        )
        return reply(output, response_id=f"response{len(calls)}")

    install(monkeypatch, respond)
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
            child = await create(*args, **kwargs)
            children.append(child)
        started.set()
        await release.wait()
        if not spawned:
            child = await create(*args, **kwargs)
            children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    supervisor = Supervisor(tmp_path, PydanticAIAdapter())
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
        assert not any(
            e.turn_id == first.turn_id
            for e in supervisor.journal.events(first.session_id, terminal_seq)
        )
        with pytest.raises(ConflictError, match="already ended"):
            supervisor.journal.append(
                first, "tool_result", {"call_id": "late", "output": "late", "is_error": False}
            )
    finally:
        release.set()
        await supervisor.close()


# --- Pydantic AI specific ----------------------------------------------------------------

RAW_WRITE = '{ "content":"line1\\nline2 \\u00e9", "path" : "exact.txt", "extra": {"n": [1, "2", null, true]} }'
EXACT_INPUT = {
    "content": "line1\nline2 é",
    "path": "exact.txt",
    "extra": {"n": [1, "2", None, True]},
}
ASK_WRITE = [
    {"kind": "builtin", "name": name, "permission": "ask" if name == "write" else "auto"}
    for name in ("bash", "read", "write", "edit")
]


@pytest.mark.parametrize("decision", ["allow", "deny", "result"])
async def test_approval_round_trip_runs_the_exact_arguments_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    bodies: list[dict[str, Any]] = []
    answered = asyncio.Event()  # holds the turn open so duplicate decisions can be tried

    async def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return reply([reasoning(), call("write", RAW_WRITE, "call-w")])
        await answered.wait()
        return reply([text("done")])

    install(monkeypatch, respond)
    async with serve_harness(tmp_path, EngineProxy(monkeypatch)) as (client, _, home):
        driver, session = await driver_session(client, tools=ASK_WRITE)
        target = workspace(home, session.ref.id) / "exact.txt"
        try:
            sent = await driver.events.send(SCOPE, session.ref, [message("go")], key="send")
            async with asyncio.timeout(10):
                while (
                    snapshot := await driver.events.reconcile(SCOPE, session.ref)
                ).state != "requires_action":
                    await asyncio.sleep(0.01)
            action = snapshot.required_actions[0]
            assert action.payload["name"] == "write"
            assert action.payload["input"] == EXACT_INPUT  # nothing coerced, dropped or renamed
            assert not target.exists()
            actions = f"/v0/turns/{sent.turn_id}/actions"
            assert (
                await client.post(actions, json={"action_id": "nope", "decision": "allow"})
            ).status_code == 404
            assert (
                await client.post(actions, json={"action_id": action.id, "decision": "result"})
            ).status_code == 422  # a result decision without a result
            answer = {
                "allow": UserToolConfirmation(action_id=action.id, decision="allow"),
                "deny": UserToolConfirmation(action_id=action.id, decision="deny"),
                "result": UserToolResult(
                    action_id=action.id, content=(TextPart(text="host-result"),)
                ),
            }[decision]
            await driver.events.send(
                SCOPE, session.ref, [answer], key="answer", expected_turn=sent.turn_id
            )
            body = {"allow": "allow", "deny": "deny", "result": "result"}[decision]
            same = {"action_id": action.id, "decision": body} | (
                {
                    "result": {
                        "content": [{"type": "text", "text": "host-result"}],
                        "is_error": False,
                    }
                }
                if decision == "result"
                else {}
            )
            assert (
                await client.post(actions, json=same)
            ).status_code == 200  # duplicate is idempotent
            other = {"action_id": action.id, "decision": "deny" if decision == "allow" else "allow"}
            assert (await client.post(actions, json=other)).status_code == 409
            answered.set()
            events = await collect(driver, session, after="0")
            assert events[-1].payload["outcome"] == "completed"
            assert sum(e.type == "agent.tool_result" for e in events) == 1
            assert sum(e.type == "agent.tool_use" for e in events) == 1
            output = next(i for i in bodies[1]["input"] if i.get("type") == "function_call_output")
            if decision == "allow":
                assert target.read_text() == "line1\nline2 é"
            else:
                assert not target.exists()
                assert (
                    "host-result" if decision == "result" else "Tool execution denied"
                ) in output["output"]
            call_item = next(i for i in bodies[1]["input"] if i.get("type") == "function_call")
            assert (
                call_item["arguments"] == RAW_WRITE
            )  # the model's argument string is replayed verbatim
            assert len(bodies) == 2
        finally:
            answered.set()
            await driver.aclose()


async def test_mcp_policy_names_wildcard_deny_and_unlisted_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mcp_server(tmp_path, monkeypatch) as mcp:
        azure = Azure(
            [
                [
                    call("sample__echo", {"text": "mcp-result"}, "mcp-call"),
                    call("sample__forbidden", {}, "denied-call"),
                    call("sample__ghost", {"x": 1}, "ghost-call"),
                    call("write", {"path": "side.txt", "content": "parallel"}, "write-call"),
                ]
            ]
        )
        install(monkeypatch, azure)
        request = TurnRequest.model_validate(turn())
        request.limits.max_tool_calls = 10
        request.tool_policy = {"sample/*": "deny", "sample/echo": "ask"}
        request.mcp_servers = [
            MCPServer(
                name="sample", url=mcp.url, headers_env={"Authorization": "HARNESS_MCP_CRED_SAMPLE"}
            )
        ]
        events, emit = collector()
        context = TurnContext(request, tmp_path, emit, credentials=turn_credentials(request, None))

        async def approve(kind: str, data: dict[str, Any]) -> None:
            await emit(kind, data)
            if kind == "requires_action":
                assert mcp.executed == []  # approval is asked before the server is called
                action = str(data["action_id"])
                context.pending[action].set_result(
                    ActionRequest(action_id=action, decision="allow")
                )

        context.emit = approve
        history = await PydanticAIAdapter().run(context, [])
        first = azure.tool_names(0)
        assert "sample__echo" in first and "sample__forbidden" not in first
        assert "sample__ghost" not in first and "write" in first
        uses = {d["call_id"]: d for d in kinds(events, "tool_use")}
        assert uses["mcp-call"] == {
            "call_id": "mcp-call",
            "name": "sample/echo",
            "input": {"text": "mcp-result"},
            "executor": "mcp",
        }
        assert (
            uses["denied-call"]["name"] == "sample/forbidden"
            and uses["denied-call"]["executor"] == "mcp"
        )
        assert (
            uses["ghost-call"]["name"] == "sample/ghost" and uses["ghost-call"]["executor"] == "mcp"
        )
        assert uses["write-call"]["executor"] == "agent"
        results = {d["call_id"]: d for d in kinds(events, "tool_result")}
        for denied in ("denied-call", "ghost-call"):
            assert results[denied]["is_error"] and "denied by policy" in results[denied]["output"]
        assert len(kinds(events, "requires_action")) == 1
        assert mcp.executed == ["mcp-result"]
        assert (tmp_path / "work/side.txt").read_text() == "parallel"
        second = azure.bodies[1]["input"]
        assert sorted(i["call_id"] for i in second if i.get("type") == "function_call") == sorted(
            i["call_id"] for i in second if i.get("type") == "function_call_output"
        )
        assert "mcp-result" in str(second) and "[redacted]" in str(second)
        assert "synthetic-operator-mcp-secret" not in str(azure.bodies) + str(events) + str(history)
        assert len(kinds(events, "usage")) == 2


async def test_mcp_connects_with_bound_headers_through_the_shared_client_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    async with mcp_server(tmp_path, monkeypatch) as mcp:
        tls_client = pydantic_engine.bound_mcp_http_client

        def recording(**kwargs: Any) -> httpx.AsyncClient:
            built.append(kwargs)
            return tls_client(**kwargs)

        monkeypatch.setattr(pydantic_engine, "bound_mcp_http_client", recording)
        install(monkeypatch, Azure([[call("sample__echo", {"text": "hi"}, "c")]]))
        request = TurnRequest.model_validate(turn())
        request.mcp_servers = [
            MCPServer(
                name="sample", url=mcp.url, headers_env={"Authorization": "HARNESS_MCP_CRED_SAMPLE"}
            )
        ]
        _, emit = collector()
        context = TurnContext(request, tmp_path, emit, credentials=turn_credentials(request, None))
        await PydanticAIAdapter().run(context, [])
    assert [kwargs["headers"] for kwargs in built] == [{"Authorization": mcp.secret}]
    assert mcp.executed == ["hi"]


# --- Retries, limits and argument handling ------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_no_retry_at_any_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls = 0
    clients: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"retry-after": "0", "x-should-retry": "true"},
            json={"error": {"message": "try again", "type": "server_error"}},
        )

    install(monkeypatch, respond, clients)
    events, emit = collector()
    with pytest.raises(HarnessError):
        await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert calls == 1 and [c["max_retries"] for c in clients] == [0]
    assert len(kinds(events, "model_request_started")) == 1


async def test_unknown_tool_fails_the_turn_without_a_hidden_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    azure = Azure([[call("not_a_tool", {}, "ghost")]])
    install(monkeypatch, azure)
    events, emit = collector()
    with pytest.raises(UnexpectedModelBehavior):
        await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert len(azure.bodies) == 1 and len(kinds(events, "usage")) == 1
    assert not kinds(events, "tool_use")


@pytest.mark.parametrize(
    "arguments",
    ["not json", "[1, 2]", '"text"', '{"command": ', ""],
    ids=["invalid-json", "array", "string", "truncated", "empty"],
)
async def test_unparseable_arguments_are_a_tool_result_not_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: str
) -> None:
    azure = Azure([[call("bash", arguments, "bad")]])
    install(monkeypatch, azure)
    events, emit = collector()
    await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert len(azure.bodies) == 2  # the call and its result; nothing extra
    output = next(i for i in azure.bodies[1]["input"] if i.get("type") == "function_call_output")
    if arguments == "":  # Pydantic AI substitutes "{}" for an empty argument string
        assert output["output"] == '"Tool failed: KeyError"'
        assert len(kinds(events, "tool_use")) == 1
    else:
        assert output["output"].startswith("An error occurred while running the tool.")
        assert not kinds(events, "tool_use")  # never reached the shell, like the Agents SDK


async def test_wrong_shape_reaches_the_shell_and_fails_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    azure = Azure([[call("write", {"path": "only-path.txt"}, "short")]])
    install(monkeypatch, azure)
    events, emit = collector()
    await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert kinds(events, "tool_use")[0]["input"] == {"path": "only-path.txt"}
    result = kinds(events, "tool_result")[0]
    # "content" defaults to empty in the shell's file tool, exactly as with the Agents SDK
    assert result["is_error"] is False and (tmp_path / "work/only-path.txt").read_text() == ""
    azure = Azure([[call("bash", {"cmd": "echo hi"}, "wrong")]])
    install(monkeypatch, azure)
    events, emit = collector()
    await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert kinds(events, "tool_result")[0] == {
        "call_id": "wrong",
        "output": "Tool failed: KeyError",
        "is_error": True,
    }


async def test_request_limit_stops_a_loop_the_shell_cannot_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic_ai.exceptions import UsageLimitExceeded

    azure = Azure([[call("bash", "not json", f"c{i}")] for i in range(20)])
    install(monkeypatch, azure)
    events, emit = collector()
    with pytest.raises(UsageLimitExceeded):
        await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert len(azure.bodies) == 3 + 2  # max_tool_calls + 2
    assert len(kinds(events, "usage")) == len(kinds(events, "model_request_started")) == 5


async def test_tool_limit_from_the_shell_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    azure = Azure(
        [[call("write", {"path": f"f{i}.txt", "content": "x"}, f"c{i}")] for i in range(10)]
    )
    install(monkeypatch, azure)
    events, emit = collector()
    with pytest.raises(HarnessError) as raised:
        await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert raised.value.kind == "tool_limit"
    assert len(kinds(events, "tool_use")) == 4 and len(azure.bodies) == 4
    assert kinds(events, "tool_result")[-1]["output"] == "Maximum tool calls exceeded"


async def test_unreachable_mcp_server_fails_before_any_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    azure = Azure()
    install(monkeypatch, azure)
    request = TurnRequest.model_validate(turn())
    request.mcp_servers = [MCPServer(name="sample", url="http://127.0.0.1:9/mcp")]
    events, emit = collector()
    with pytest.raises(httpx.ConnectError):  # the Agents SDK engine raises its UserError here
        await PydanticAIAdapter().run(TurnContext(request, tmp_path, emit), [])
    assert not azure.bodies and not events


async def test_tool_limit_over_mcp_is_not_wrapped_in_an_exception_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with mcp_server(tmp_path, monkeypatch) as mcp:
        azure = Azure([[call("sample__echo", {"text": str(i)}, f"c{i}")] for i in range(10)])
        install(monkeypatch, azure)
        request = TurnRequest.model_validate(turn())
        request.mcp_servers = [MCPServer(name="sample", url=mcp.url)]
        _, emit = collector()
        with pytest.raises(HarnessError) as raised:
            await PydanticAIAdapter().run(TurnContext(request, tmp_path, emit), [])
    assert raised.value.kind == "tool_limit"


# --- Messages, history and input -------------------------------------------------------


async def test_each_assistant_message_gets_one_delta_and_one_final_with_a_shared_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(
        monkeypatch,
        Azure([[text("first", "m1"), text("second", "m2"), call("read", {"path": "x"}, "r")]]),
    )
    events, emit = collector()
    await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    deltas, finals = kinds(events, "agent_message_delta"), kinds(events, "agent_message")
    assert [d["text"] for d in deltas] == ["first", "second", "done"]
    assert [d["message_id"] for d in deltas] == [f["message_id"] for f in finals]
    assert len({f["message_id"] for f in finals}) == 3
    assert [f["parts"] for f in finals][0] == [{"type": "text", "text": "first"}]


async def test_text_and_image_input_and_skills_reach_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mux.drivers.selfhosted.wire import InlineSkill

    azure = Azure()
    install(monkeypatch, azure)
    request = TurnRequest.model_validate(turn())
    request.input = TurnRequest.model_validate(
        turn()
        | {
            "input": [
                {"type": "text", "text": "look"},
                {"type": "image", "media_type": "image/png", "data_b64": "aGk="},
            ]
        }
    ).input
    request.skills = [InlineSkill(name="analysis", files={"SKILL.md": "be careful"})]
    _, emit = collector()
    context = TurnContext(request, tmp_path, emit)
    await PydanticAIAdapter().run(context, [])
    body = azure.bodies[0]
    assert body["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,aGk=",
                    "detail": "auto",
                },
            ],
        }
    ]
    assert body["instructions"].startswith("Help") and "be careful" in body["instructions"]
    assert context.model_input  # set for every request, as the Agents SDK engine does


async def test_persisted_history_has_no_instructions_and_keeps_encrypted_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    azure = Azure([[reasoning("rs-a", "enc-a"), text("answer", "m")]])
    install(monkeypatch, azure)
    _, emit = collector()
    history = await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert "Help" not in json.dumps(history) and "enc-a" in json.dumps(history)
    # A later turn (a different engine instance, as after a restart) replays it unchanged.
    follow = Azure()
    install(monkeypatch, follow)
    await PydanticAIAdapter().run(
        TurnContext(TurnRequest.model_validate(turn(turn_id="two", key="two")), tmp_path, emit),
        history,
    )
    items = follow.bodies[0]["input"]
    assert follow.input_types(0) == ["user", "reasoning", "message", "user"]
    assert items[1]["encrypted_content"] == "enc-a" and items[1]["id"] == "rs-a"
    assert items[2]["id"] == "m" and items[2]["content"][0]["text"] == "answer"


def test_foreign_history_items_are_refused_not_guessed() -> None:
    with pytest.raises(HarnessError) as raised:
        pydantic_engine.load_history([{"type": "function_call", "call_id": "c", "name": "bash"}])
    assert raised.value.kind == "history_unsupported"


# --- Cancellation and restart --------------------------------------------------------------


@pytest.mark.parametrize("stage", ["model", "tool"])
async def test_cancel_then_restart_never_reruns_the_model_or_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    calls: list[dict[str, Any]] = []
    in_model = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            if stage == "model":
                in_model.set()
                await asyncio.Event().wait()
            return reply([call("bash", {"command": "echo run >> counter.txt; sleep 30"}, "slow")])
        return reply([text("done")])

    install(monkeypatch, respond)
    request = TurnRequest.model_validate(turn())
    supervisor = Supervisor(tmp_path, PydanticAIAdapter())
    try:
        await supervisor.submit(request)
        sandbox, _ = supervisor.journal.sandbox(request.session_id)
        counter = sandbox / "work/counter.txt"
        async with asyncio.timeout(10):
            while not (in_model.is_set() if stage == "model" else counter.exists()):
                await asyncio.sleep(0.02)
        supervisor.tasks[request.turn_id].cancel()
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 10)
        events = supervisor.journal.events(request.session_id)
        assert events[-1].type == "turn_ended" and events[-1].data["outcome"] == "interrupted"
        assert not supervisor.journal.pending_requests(request)
        assert len(calls) == 1
    finally:
        await supervisor.close()

    restarted = Supervisor(tmp_path, PydanticAIAdapter())
    try:
        restarted.recover()
        await restarted.submit(request)  # the same request again is a replay, not a new run
        assert request.turn_id not in restarted.tasks and len(calls) == 1
        if stage == "tool":
            assert counter.read_text() == "run\n"
        follow_up = TurnRequest.model_validate(turn(turn_id="next", key="next"))
        await restarted.submit(follow_up)
        await asyncio.wait_for(restarted.tasks["next"], 10)
        assert restarted.journal.events(follow_up.session_id)[-1].data["outcome"] == "completed"
        sent = [str(item) for item in calls[1]["input"]]
        assert any("Interrupted turn input" in item for item in sent)
        assert any("Interrupted-turn tool record" in item for item in sent) == (stage == "tool")
        if stage == "tool":
            assert counter.read_text() == "run\n"  # the interrupted tool was not run again
    finally:
        await restarted.close()


# --- Same wire as the Agents SDK engine ----------------------------------------------------


def normalized(body: dict[str, Any]) -> dict[str, Any]:
    """Drop the differences that are defaults or whitespace: Pydantic AI sends ``stream`` and
    ``tool_choice`` explicitly, omits ``status`` on replayed calls and trims instructions."""
    body = {
        k: v for k, v in body.items() if k != "stream" and not (k == "tool_choice" and v == "auto")
    }
    body["instructions"] = body["instructions"].strip()
    body["input"] = [
        {
            k: v
            for k, v in item.items()
            if not (item.get("type") == "function_call" and k == "status")
        }
        for item in body["input"]
    ]
    return body


def scripted_conversation(turn_number: int) -> list[list[dict[str, object]]]:
    uid = f"-{turn_number}"
    return [
        [
            reasoning("rs" + uid, "enc" + uid)
            | {"summary": [{"type": "summary_text", "text": "thinking"}]},
            text("let me write", "msg0" + uid) | {"phase": "commentary"},
            call("write", {"path": "a.txt", "content": "x"}, "w" + uid),
        ],
        [reasoning("rs2" + uid, "enc2" + uid), call("bash", {"command": "echo hi"}, "b" + uid)],
        [text("saved", "msg1" + uid)],
    ]


def mock_azure_client(handler: Azure) -> Any:
    return lambda **kw: AsyncAzureOpenAI(
        **kw, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def run_conversation(
    engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[dict[str, Any]], Events]:
    request = TurnRequest.model_validate(turn())
    request.input = TurnRequest.model_validate(
        turn()
        | {
            "input": [
                {"type": "text", "text": "hello"},
                {"type": "image", "media_type": "image/png", "data_b64": "aGk="},
            ]
        }
    ).input
    request.tool_policy = {"edit": "deny"}
    bodies: list[dict[str, Any]] = []
    events, emit = collector()
    history: list[Any] = [{"role": "user", "content": "earlier"}]
    for number in (1, 2):
        azure = Azure(scripted_conversation(number))
        if engine == "agents-sdk":
            monkeypatch.setattr(model_adapter, "AsyncAzureOpenAI", mock_azure_client(azure))
            monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
            monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://unit-test.openai.azure.com")
            adapter: model_adapter.AzureModelAdapter = model_adapter.AzureModelAdapter()
        else:
            install(monkeypatch, azure)
            adapter = PydanticAIAdapter()
        context = TurnContext(request.model_copy(update={"turn_id": f"t{number}"}), tmp_path, emit)
        history = await adapter.run(context, history)
        bodies += azure.bodies
    return bodies, events


async def test_requests_and_events_match_the_agents_sdk_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk_bodies, sdk_events = await run_conversation("agents-sdk", tmp_path / "sdk", monkeypatch)
    pyd_bodies, pyd_events = await run_conversation("pydantic-ai", tmp_path / "pyd", monkeypatch)
    assert len(sdk_bodies) == len(pyd_bodies) == 6
    for sdk, pyd in zip(sdk_bodies, pyd_bodies, strict=True):
        assert normalized(pyd) == normalized(sdk)
        assert "edit" not in [tool["name"] for tool in pyd["tools"]]

    def shape(events: Events) -> Events:
        return [
            (kind, {k: v for k, v in data.items() if k not in {"request_id", "message_id"}})
            for kind, data in events
        ]

    assert shape(pyd_events) == shape(sdk_events)


# --- Where Pydantic AI's loop differs from the Agents SDK loop ------------------------------


@pytest.mark.parametrize(
    "output",
    [[], [reasoning("rs-only", "enc")]],
    ids=["empty", "reasoning-only"],
)
async def test_response_with_nothing_to_act_on_errors_instead_of_resubmitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: list[dict[str, object]]
) -> None:
    """The Agents SDK ends such a turn with empty text; Pydantic AI would resubmit the request
    (retry budget 0 turns that into an error). Either way no extra request is made."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    azure = Azure([output])
    install(monkeypatch, azure)
    events, emit = collector()
    with pytest.raises(UnexpectedModelBehavior, match="maximum output retries"):
        await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert len(azure.bodies) == 1 and len(kinds(events, "usage")) == 1


async def test_refusal_is_a_content_filter_error_with_usage_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic_ai.exceptions import ContentFilterError

    refusal = text("", "msg") | {"content": [{"type": "refusal", "refusal": "I cannot help"}]}
    azure = Azure([[refusal]])
    install(monkeypatch, azure)
    events, emit = collector()
    with pytest.raises(ContentFilterError):
        await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    assert len(azure.bodies) == 1 and len(kinds(events, "usage")) == 1


async def test_usage_without_details_reports_zero_cached_and_reasoning_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return reply([text("ok")], usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})

    install(monkeypatch, respond)
    events, emit = collector()
    await PydanticAIAdapter().run(context_for(tmp_path, emit), [])
    usage = kinds(events, "usage")[0]
    assert (usage["input_tokens"], usage["cached_input_tokens"]) == (2, 0)
    assert (usage["output_tokens"], usage["reasoning_tokens"]) == (1, 0)


async def test_deadline_interrupts_a_hung_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    install(monkeypatch, respond)
    request = TurnRequest.model_validate(turn())
    request.limits.deadline_s = 1
    supervisor = Supervisor(tmp_path, PydanticAIAdapter())
    try:
        await supervisor.submit(request)
        await asyncio.wait_for(supervisor.tasks[request.turn_id], 10)
        events = supervisor.journal.events(request.session_id)
        assert events[-1].type == "turn_ended" and events[-1].data["outcome"] == "interrupted"
        assert not supervisor.journal.pending_requests(request)
        assert any(e.data.get("kind") == "usage_unavailable" for e in events if e.type == "error")
    finally:
        await supervisor.close()
