"""Mocked Azure Responses endpoint and MCP server for the Pydantic AI engine tests."""

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

import httpx
import pytest
import uvicorn
from daimon_harness import pydantic_engine
from daimon_harness.runtime import TurnContext
from mcp.server.fastmcp import FastMCP
from openai import AsyncAzureOpenAI
from pydantic import JsonValue
from sse_starlette.sse import _get_shutdown_state  # pyright: ignore[reportPrivateUsage]

USAGE = {
    "input_tokens": 12,
    "input_tokens_details": {"cached_tokens": 3},
    "output_tokens": 8,
    "output_tokens_details": {"reasoning_tokens": 2},
    "total_tokens": 20,
}
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def text(content: str, item_id: str = "msg") -> dict[str, object]:
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": content, "annotations": []}],
    }


def call(name: str, arguments: object, call_id: str = "call") -> dict[str, object]:
    return {
        "id": "fc-" + call_id,
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "status": "completed",
        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
    }


def reasoning(item_id: str = "rs", encrypted: str = "opaque-reasoning") -> dict[str, object]:
    return {"id": item_id, "type": "reasoning", "summary": [], "encrypted_content": encrypted}


def reply(
    output: list[dict[str, object]], *, response_id: str = "resp", **fields: object
) -> httpx.Response:
    body: dict[str, object] = {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-luna",
        "status": "completed",
        "output": output,
        "usage": USAGE,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    return httpx.Response(200, json=body | fields)


@dataclass
class Azure:
    """Scripted Azure: ``outputs[i]`` answers request ``i``; later requests get a final message."""

    outputs: list[list[dict[str, object]]] = field(default_factory=list[list[dict[str, object]]])
    bodies: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        index = len(self.bodies) - 1
        output = self.outputs[index] if index < len(self.outputs) else [text("done")]
        return reply(output, response_id=f"resp-{len(self.bodies)}")

    def tool_names(self, index: int) -> list[str]:
        return [tool["name"] for tool in self.bodies[index].get("tools", [])]  # type: ignore[index, union-attr]

    def input_types(self, index: int) -> list[str]:
        items = self.bodies[index]["input"]
        return [str(item.get("type") or item.get("role")) for item in items]  # type: ignore[union-attr]


def install(
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler,
    clients: list[dict[str, object]] | None = None,
) -> None:
    """Replace the engine's Azure client with one speaking to ``handler`` (a fresh client per
    turn, closed by the engine like the real one)."""

    def factory(**kwargs: object) -> AsyncAzureOpenAI:
        if clients is not None:
            clients.append(kwargs)
        return AsyncAzureOpenAI(
            **kwargs,  # type: ignore[arg-type]
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(pydantic_engine, "AsyncAzureOpenAI", factory)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://unit-test.openai.azure.com")


class EngineProxy:
    """``create_app`` refuses ``AzureModelAdapter`` instances and configured credentials in
    test mode. Build the engine first (it reads the environment), then hide both."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine = pydantic_engine.PydanticAIAdapter()
        monkeypatch.delenv("AZURE_OPENAI_API_KEY")
        monkeypatch.delenv("HARNESS_MCP_CRED_SAMPLE", raising=False)

    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
        return await self.engine.run(context, history)


@dataclass
class McpFixture:
    port: int
    executed: list[str]
    secret: str

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/mcp"

    @property
    def bindings(self) -> str:
        origin = f"https://127.0.0.1:{self.port}"
        return json.dumps([{"name": "HARNESS_MCP_CRED_SAMPLE", "origin": origin, "tenants": ["*"]}])


@asynccontextmanager
async def mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[McpFixture]:
    """A local HTTPS streamable-HTTP MCP server with ``echo`` and ``forbidden`` tools
    (the same shape as ``mcp_probe``, whose server is not reusable on its own)."""
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit", False)
    secret = "Bearer synthetic-operator-mcp-secret"
    monkeypatch.setenv("HARNESS_MCP_CRED_SAMPLE", secret)
    executed: list[str] = []
    mcp = FastMCP("unit-test", stateless_http=True)

    @mcp.tool()
    def echo(text: str) -> str:
        executed.append(text)
        return text + " " + secret

    @mcp.tool()
    def forbidden() -> str:
        raise AssertionError("denied tool must not execute")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    certificate, private_key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            mcp.streamable_http_app(),
            log_level="error",
            ssl_certfile=str(certificate),
            ssl_keyfile=str(private_key),
        )
    )

    def local_tls_client(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            verify=ssl.create_default_context(cafile=str(certificate)),
            follow_redirects=False,
        )

    monkeypatch.setattr(pydantic_engine, "bound_mcp_http_client", local_tls_client)
    monkeypatch.setenv("HARNESS_MCP_CRED_BINDINGS", McpFixture(port, executed, secret).bindings)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield McpFixture(port, executed, secret)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
        # sse-starlette's watcher polls twice a second for the stopped server and would mark
        # the next fixture's server as shut down; let it finish first.
        async with asyncio.timeout(5):
            while _get_shutdown_state().watcher_started:
                await asyncio.sleep(0.05)


@asynccontextmanager
async def loopback_azure(
    echo: str, calls: list[dict[str, object]], errors: list[str]
) -> AsyncIterator[str]:
    """A real TCP Azure Responses stub that echoes ``echo`` as the model (the C12 check)."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(10):
                header = (await reader.readuntil(b"\r\n\r\n")).decode()
                length = next(
                    int(line.split(":", 1)[1])
                    for line in header.split("\r\n")
                    if line.lower().startswith("content-length:")
                )
                calls.append(json.loads(await reader.readexactly(length)))
                payload = reply([text("ready", "msg-probe")], response_id="resp-probe", model=echo)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload.content)}\r\nConnection: close\r\n\r\n".encode()
                    + payload.content
                )
                await writer.drain()
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
