"""Legacy turn calls bound SDK retry stacking and whole paginator walks."""

import asyncio

import httpx
import pytest
from anthropic import APIConnectionError, AsyncAnthropic, InternalServerError
from mux.drivers.anthropic import transport
from mux.drivers.anthropic.transport import LegacyTurnTransport, single_attempt_mutation


@pytest.mark.parametrize(
    "operation", ["status", "replay", "latest_idle_is_settled", "send", "archive"]
)
async def test_non_streaming_calls_cancel_a_hung_request(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(transport, "NON_STREAMING_TIMEOUT_S", 0.1)
    cancelled = asyncio.Event()
    requests: list[httpx.Request] = []

    async def hang(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("request should have been cancelled")

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(hang)),
    ) as client:
        io = LegacyTurnTransport(client, "sess_1")
        with pytest.raises(TimeoutError):
            if operation == "send":
                await io.send([{"type": "user.message", "content": "hi"}])
            else:
                await getattr(io, operation)()
    assert cancelled.is_set()
    assert len(requests) == 1
    assert requests[0].extensions["timeout"]["read"] == 30.0


async def test_claimed_mutations_keep_zero_retries_and_short_timeout() -> None:
    attempts: list[httpx.Request] = []

    async def fail(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("offline", request=request)

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    ) as client:
        io = LegacyTurnTransport(client, "sess_1")
        with single_attempt_mutation(), pytest.raises(APIConnectionError):
            await io.send([{"type": "user.message", "content": "hi"}])
    assert len(attempts) == 1
    assert attempts[0].extensions["timeout"]["read"] == 30.0


async def test_status_uses_two_sdk_retries_even_when_shared_client_uses_eight() -> None:
    attempts: list[httpx.Request] = []

    async def fail(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(500, headers={"retry-after-ms": "1"}, json={"error": "oops"})

    async with AsyncAnthropic(
        api_key="test",
        max_retries=8,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    ) as client:
        with pytest.raises(InternalServerError):
            await LegacyTurnTransport(client, "sess_1").status()
        assert client.max_retries == 8
    assert len(attempts) == 3
    assert all(request.extensions["timeout"]["read"] == 30.0 for request in attempts)


async def test_explicit_zero_retry_client_stays_single_attempt() -> None:
    attempts: list[httpx.Request] = []

    async def fail(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("offline", request=request)

    async with AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    ) as client:
        with pytest.raises(APIConnectionError):
            await LegacyTurnTransport(client, "sess_1").send(
                [{"type": "user.message", "content": "hi"}]
            )
    assert len(attempts) == 1
