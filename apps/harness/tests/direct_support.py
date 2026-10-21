"""Synthetic model payloads, a scripted model and a local MCP server for the direct-engine tests."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from anthropic import AsyncAnthropic
from daimon_harness import direct_engine
from daimon_harness.direct_engine import DirectResponsesAdapter
from mcp.server.fastmcp import FastMCP
from openai import AsyncAzureOpenAI

ENDPOINT = "https://unit-test.openai.azure.com"
SECRET = "Bearer synthetic-operator-mcp-secret"


def usage_json(input_tokens: int = 12, cached: int = 3, output: int = 8, reasoning: int = 2):
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input_tokens + output,
    }


def response_json(output: list[dict[str, Any]], *, id: str = "resp", **fields: Any):
    """A synthetic Azure Responses payload; `fields` override or remove (None) top-level keys."""
    body = {
        "id": id,
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-luna",
        "status": "completed",
        "output": output,
        "usage": usage_json(),
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    return body | fields


def message_item(text: str = "done", **fields: Any) -> dict[str, Any]:
    return {
        "id": "msg",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    } | fields


def call_item(name: str, arguments: dict[str, Any] | str, call_id: str = "call-1", **fields: Any):
    return {
        "id": f"fc-{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        "status": "completed",
    } | fields


def reasoning_item(encrypted: str = "encrypted-reasoning", id: str = "rs_1") -> dict[str, Any]:
    return {"id": id, "type": "reasoning", "summary": [], "encrypted_content": encrypted}


class Script:
    """Answers the n-th model request with the n-th scripted payload and records every body."""

    def __init__(
        self,
        *outputs: Any,
        before: Callable[[int, dict[str, Any]], Awaitable[None]] | None = None,
        path: str = "/responses",
        payload: Callable[[Any, int], dict[str, Any]] = lambda output, count: response_json(
            output, id=f"resp_{count}"
        ),
    ) -> None:
        self.outputs = outputs
        self.before = before
        self.path = path
        self.payload = payload
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host in {"unit-test.openai.azure.com", "api.anthropic.com"}
        assert request.url.path.endswith(self.path)
        self.bodies.append(json.loads(request.content))
        count = len(self.bodies)
        if self.before is not None:
            await self.before(count, self.bodies[-1])
        return httpx.Response(200, json=self.payload(self.outputs[count - 1], count))


def install_azure(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[..., Any], module: Any = direct_engine
) -> None:
    """Point an engine module's Azure client constructor at a mocked HTTP transport."""

    def build(**kwargs: Any) -> AsyncAzureOpenAI:
        assert kwargs["max_retries"] == 0
        transport = httpx.MockTransport(handler)
        return AsyncAzureOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport))

    monkeypatch.setattr(module, "AsyncAzureOpenAI", build)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")


def serving_adapter(monkeypatch: pytest.MonkeyPatch) -> DirectResponsesAdapter:
    """An adapter the service accepts in test mode: it captured its key, so the env can go."""
    adapter = DirectResponsesAdapter()
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    return adapter


@dataclass
class McpFixture:
    url: str
    origin: str
    executed: list[str] = field(default_factory=list[str])
    slow_started: asyncio.Event = field(default_factory=asyncio.Event)
    headers: list[dict[str, str]] = field(default_factory=list[dict[str, str]])


@asynccontextmanager
async def mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, leak_secret: bool = False
) -> AsyncIterator[McpFixture]:
    """A local HTTPS MCP server: `echo`, `slow` (blocks), and a `forbidden` tool that must not run."""
    from sse_starlette.sse import AppStatus

    # sse-starlette keeps a process-wide shutdown flag, and its per-loop watcher copies the flag
    # of an earlier test's stopped uvicorn into it. Tests share one loop, so own the flag here.
    monkeypatch.setattr(AppStatus, "should_exit", False)
    monkeypatch.setattr(AppStatus, "enable_automatic_graceful_drain", False)
    mcp = FastMCP("unit-test", stateless_http=True)
    seen = McpFixture(url="", origin="")

    @mcp.tool()
    def echo(text: str) -> str:
        seen.executed.append(text)
        return text + " " + SECRET if leak_secret else text

    @mcp.tool()
    def forbidden() -> str:
        raise AssertionError("denied tool must not execute")

    @mcp.tool()
    async def slow() -> str:
        seen.slow_started.set()
        await asyncio.sleep(30)
        return "late"

    app = mcp.streamable_http_app()

    async def recording(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            seen.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await app(scope, receive, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    seen.origin = f"https://127.0.0.1:{port}"
    seen.url = seen.origin + "/mcp"
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
        + ["-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"]
        + ["-keyout", str(key), "-out", str(certificate)],
        check=True,
        capture_output=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            recording, log_level="error", ssl_certfile=str(certificate), ssl_keyfile=str(key)
        )
    )

    def trusting_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
        context = ssl.create_default_context(cafile=str(certificate))
        return httpx.AsyncClient(
            headers=headers, timeout=timeout, auth=auth, verify=context, follow_redirects=False
        )

    monkeypatch.setattr(direct_engine, "bound_mcp_http_client", trusting_client)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield seen
    finally:
        AppStatus.should_exit = (
            True  # end open SSE streams so uvicorn can stop; undone by monkeypatch
        )
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()


# --- Anthropic Messages payloads ------------------------------------------------------------------


def message_json(content: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    """A synthetic Messages payload; `fields` override top-level keys such as stop_reason."""
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    } | fields


def anthropic_script(
    *messages: dict[str, Any],
    before: Callable[[int, dict[str, Any]], Awaitable[None]] | None = None,
) -> Script:
    return Script(*messages, before=before, path="/v1/messages", payload=lambda m, _: m)


def install_anthropic(monkeypatch: pytest.MonkeyPatch, handler: Callable[..., Any]) -> None:
    def build(**kwargs: Any) -> AsyncAnthropic:
        assert kwargs["max_retries"] == 0
        transport = httpx.MockTransport(handler)
        return AsyncAnthropic(**kwargs, http_client=httpx.AsyncClient(transport=transport))

    monkeypatch.setattr(direct_engine, "AsyncAnthropic", build)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
