"""An interaction may echo our MCP server's Authorization header: scrub it, refuse the rest.

Whether Gemini echoes request tool headers is unverified (F2-G r12 evidence
dropped all headers before saving), so the transport assumes it may, exactly
as it does for the storage-mount transform: only our own header, at
`tools[i].headers` of an `mcp_server` tool, is removed; the token anywhere
else, or any other header, fails closed without quoting it.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.drivers.gemini import transport as transport_module
from mux.drivers.gemini.mcp_auth import RedactedCredential
from mux.drivers.gemini.transport import (
    Object,
    SDKTransport,
    checked_storage_response,
    mcp_request_tokens,
    scrub_mcp_echo,
)
from mux.errors import ProviderError

TOKEN = RedactedCredential("offline-mcp-token-never-escapes")
OURS = {"Authorization": "Bearer " + TOKEN}
URL = "https://mcp.invalid/mcp"


def mcp_tool(headers: object = OURS) -> Object:
    tool: Object = {
        "type": "mcp_server",
        "name": "daimon_mcp",
        "url": URL,
        "allowed_tools": ["get_agent"],
    }
    if headers is not None:
        tool["headers"] = headers  # pyright: ignore[reportArgumentType]
    return tool


def completed(*, tools: list[Object] | None = None, text: str = "final answer") -> Object:
    return {
        "id": "native",
        "status": "completed",
        "created": "2026-10-22T15:02:25Z",
        "updated": "2026-10-22T15:08:12Z",
        "tools": tools if tools is not None else [mcp_tool()],
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
        "usage": {"total_input_tokens": 10, "total_output_tokens": 2},
    }


def test_only_dispatched_mcp_bearer_headers_are_remembered() -> None:
    request: Object = {
        "tools": [
            mcp_tool(),
            {"type": "google_search"},
            {
                "type": "mcp_server",
                "name": "x",
                "url": URL,
                "headers": {"Authorization": "Basic x"},
            },
        ]
    }
    assert mcp_request_tokens(request) == {TOKEN}
    assert mcp_request_tokens({"tools": "not-a-list"}) == set()


def test_our_echoed_header_is_scrubbed_and_the_answer_survives() -> None:
    clean = checked_storage_response(completed(), protected=False, mcp_tokens={TOKEN})
    assert "headers" not in clean["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]
    assert clean["steps"][0]["content"][0]["text"] == "final answer"  # pyright: ignore[reportIndexIssue,reportArgumentType]
    assert TOKEN not in json.dumps(clean)
    nested = scrub_mcp_echo({"event_type": "x", "interaction": completed()}, {TOKEN})
    assert "headers" not in nested["interaction"]["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]


@pytest.mark.parametrize(
    "tools",
    [
        [mcp_tool({"Authorization": "Bearer someone-else"})],
        [mcp_tool({"Authorization": "Bearer " + TOKEN, "X-Extra": "1"})],
        [{"type": "google_search", "headers": OURS}],
    ],
)
def test_a_header_that_is_not_our_exact_echo_refuses(tools: list[Object]) -> None:
    with pytest.raises(ProviderError) as refused:
        checked_storage_response(completed(tools=tools), protected=False, mcp_tokens={TOKEN})
    assert refused.value.native_code == "mcp_header_echo"
    assert TOKEN not in str(refused.value)


def test_the_token_anywhere_else_refuses_without_quoting_it() -> None:
    with pytest.raises(ProviderError) as refused:
        checked_storage_response(
            completed(text="leaked Bearer " + TOKEN), protected=False, mcp_tokens={TOKEN}
        )
    assert refused.value.native_code == "mcp_token_echo"
    assert TOKEN not in str(refused.value) and TOKEN not in repr(refused.value)


def test_without_mcp_tokens_an_unprotected_response_is_untouched() -> None:
    body = completed(tools=[mcp_tool(None)])
    assert checked_storage_response(body, protected=False) is body


@asynccontextmanager
async def transport(
    respond: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[SDKTransport]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        try:
            yield SDKTransport(client)
        finally:
            await client.aio.aclose()


async def test_a_dispatched_mcp_credential_protects_create_and_later_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def already_bound(request: Object) -> Object:
        return request

    monkeypatch.setattr(transport_module, "refresh_bound_request", already_bound)
    sent: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json=completed())

    async with transport(respond) as gemini:
        created = await gemini.create(
            {"agent": "agents/base", "agent_config": {"type": "antigravity"}, "tools": [mcp_tool()]}
        )
        assert "headers" not in created["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]
        read = await gemini.get("native")
        assert "headers" not in read["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]
    assert TOKEN.encode() in sent[0], "the credential is sent to the provider"
