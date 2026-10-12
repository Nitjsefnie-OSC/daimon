"""Byte controls for main #824 (07331818), through the scoped native page port.

Every SDK uses an explicit MockTransport. The reference query/reset below is
main's original _open payload and exception boundary, kept as a test control.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from anthropic import BadRequestError, NotFoundError, omit
from daimon.core.mux_backend import resource_scope
from daimon.core.usage_reconcile import _native_history, _open
from daimon.testing.isolated_sdk import build_async_anthropic
from mux.contracts.ids import Scope
from mux.drivers.anthropic.resources.session_tools import EventQuery
from mux.errors import ScopeViolation

AT = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
SESSION = "sess-native-page"
SCOPE = resource_scope(
    tenant_id="test-tenant", account_id="test-account", authorization_id="test-owned"
)
EVENT = {
    "id": "span-wire-1",
    "type": "span.model_request_end",
    "processed_at": AT.isoformat(),
    "is_error": False,
    "model_request_start_id": "span-start",
    "model_usage": {
        "input_tokens": 2,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
}


async def wire(
    *, ported, fields=None, errors=(), resume=None, empty=False, cursor="next", continue_page=False
):
    requests = []

    def handle(request):
        requests.append(
            (request.method, request.url.raw_path, tuple(request.headers.raw), request.content)
        )
        index = len(requests) - 1
        if index < len(errors):
            status = errors[index]
            return httpx.Response(
                status,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error" if status == 400 else "not_found_error",
                        "message": "cursor rejected",
                    },
                },
            )
        token = request.url.params.get("page")
        return httpx.Response(
            200,
            json={
                "data": [] if empty else [EVENT],
                "next_page": None if token == "next" else cursor,
            },
        )

    client = build_async_anthropic(transport=httpx.MockTransport(handle))
    result = []
    failure = None
    try:
        if fields is not None:
            kwargs = {"order": "asc", "limit": 2, "types": ["span.model_request_end"], **fields}
            if ported:
                page = await _native_history(client, SCOPE, SESSION).read(EventQuery(**kwargs))
            else:
                page = await client.beta.sessions.events.list(SESSION, **kwargs)
        elif ported:
            page = await _open(
                _native_history(client, SCOPE, SESSION),
                SESSION,
                page_size=2,
                since=AT,
                lt=AT + timedelta(days=1),
                token=resume,
            )
        else:
            # Exact original main #824 query: omission uses the SDK sentinel.
            async def read(token):
                return await client.beta.sessions.events.list(
                    SESSION,
                    order="asc",
                    limit=2,
                    types=["span.model_request_end"],
                    created_at_gte=AT,
                    created_at_lt=AT + timedelta(days=1),
                    page=token if token is not None else omit,
                )

            if resume is None:
                page = await read(None)
            else:
                try:
                    page = await read(resume)
                except (BadRequestError, NotFoundError):
                    page = await read(None)
        result.append(
            (
                page.has_next_page(),
                page.next_page,
                [item.model_dump(mode="json", exclude_unset=True) for item in page.data],
            )
        )
        if continue_page and page.has_next_page():
            page = await page.get_next_page()
            result.append(
                (
                    page.has_next_page(),
                    page.next_page,
                    [item.model_dump(mode="json", exclude_unset=True) for item in page.data],
                )
            )
    except (BadRequestError, NotFoundError) as error:
        failure = (type(error).__name__, error.status_code)
    finally:
        await client.close()
    return requests, result, failure


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {
            "created_at_gte": AT.isoformat(),
            "created_at_lt": (AT + timedelta(hours=1)).isoformat(),
            "page": "saved",
        },
        {"created_at_gte": AT, "created_at_lt": AT + timedelta(hours=1), "page": "saved"},
        {"created_at_gte": None, "created_at_lt": None, "page": None},
    ],
)
async def test_native_query_request_bytes_are_sdk_equal(fields):
    expected = await wire(ported=False, fields=fields)
    actual = await wire(ported=True, fields=fields)
    assert actual == expected
    assert any(name.lower() == b"anthropic-beta" for name, _ in actual[0][0][2])


@pytest.mark.parametrize(
    "initial",
    [
        {},
        {"created_at_gte": AT, "created_at_lt": AT + timedelta(days=1)},
        {"page": "saved", "created_at_gte": AT},
    ],
)
async def test_native_continuation_is_byte_equal_and_keeps_original_filters(initial):
    actual = await wire(ported=True, fields=initial, continue_page=True)
    expected = await wire(ported=False, fields=initial, continue_page=True)
    assert [r[1] for r in actual[0]] == [r[1] for r in expected[0]]
    assert actual == expected


@pytest.mark.parametrize("errors", [(400,), (404,), (400, 404), (404, 400)])
async def test_saved_cursor_restarts_on_exact_sdk_errors(errors):
    actual = await wire(ported=True, errors=errors, resume="expired")
    assert actual == await wire(ported=False, errors=errors, resume="expired")
    assert len(actual[0]) == 2


@pytest.mark.parametrize("empty,cursor", [(True, "next"), (False, ""), (False, None)])
async def test_empty_page_or_cursor_stops_like_sdk(empty, cursor):
    actual = await wire(ported=True, fields={}, empty=empty, cursor=cursor, continue_page=True)
    assert actual == await wire(
        ported=False, fields={}, empty=empty, cursor=cursor, continue_page=True
    )
    assert len(actual[0]) == 1


@pytest.mark.parametrize(
    "scope",
    [
        None,
        Scope.platform(reason="test-workspace"),
        Scope.legacy_host_authorized(call_site="test-legacy"),
    ],
)
async def test_missing_or_unowned_scope_refuses_before_request(scope):
    requests = []
    client = build_async_anthropic(transport=httpx.MockTransport(lambda r: requests.append(r)))
    try:
        with pytest.raises(ScopeViolation):
            _native_history(client, scope, SESSION)
        assert not requests
    finally:
        await client.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "foreign"),
        ("account_id", "foreign"),
        ("provider", "gemini"),
        ("id", "other-session"),
    ],
)
async def test_foreign_reference_cannot_read_native_page(field, value):
    requests = []
    client = build_async_anthropic(transport=httpx.MockTransport(lambda r: requests.append(r)))
    try:
        history = _native_history(client, SCOPE, SESSION)
        from dataclasses import replace

        history = replace(history, session=history.session.model_copy(update={field: value}))
        with pytest.raises(ScopeViolation):
            await history.read(EventQuery())
        assert not requests
    finally:
        await client.close()


async def test_native_callback_is_private_and_json_cannot_recreate_authority():
    from mux.drivers.anthropic.resources.session_tools import EventPage

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [EVENT], "next_page": "next"})

    client = build_async_anthropic(transport=httpx.MockTransport(handle))
    history = _native_history(client, SCOPE, SESSION)
    page = await history.port.list_events(SCOPE, history.session, EventQuery())
    try:
        encoded = page.model_dump_json()
        assert "_continue" not in encoded and "test-isolated-sdk" not in encoded
        assert "test-isolated-sdk" not in repr(page)
        clone = EventPage.model_validate_json(encoded)
        with pytest.raises(RuntimeError, match="authorized native"):
            await clone.continue_page()
        assert len(requests) == 1
    finally:
        await client.close()


async def test_native_continuation_rechecks_authorization(monkeypatch):
    from mux.drivers.anthropic.resources._authorization import ResourceAuthorization

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [EVENT], "next_page": "next"})

    client = build_async_anthropic(transport=httpx.MockTransport(handle))
    try:
        history = _native_history(client, SCOPE, SESSION)
        page = await history.read(EventQuery())
        foreign = SCOPE.model_copy(update={"account_id": "foreign"})
        monkeypatch.setattr(
            history.port,
            "_authorization",
            ResourceAuthorization(foreign, frozenset({("session", SESSION)})),
        )
        with pytest.raises(ScopeViolation):
            await page.get_next_page()
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("status", [400, 404])
async def test_midpass_failure_propagates_without_cursor_reset(status):
    traces = []
    failures = []
    for ported in (False, True):
        requests = []

        def handle(request, requests=requests):
            requests.append(
                (request.method, request.url.raw_path, tuple(request.headers.raw), request.content)
            )
            if len(requests) == 1:
                return httpx.Response(200, json={"data": [EVENT], "next_page": "next"})
            return httpx.Response(
                status,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error" if status == 400 else "not_found_error",
                        "message": "cursor rejected",
                    },
                },
            )

        client = build_async_anthropic(transport=httpx.MockTransport(handle))
        try:
            if ported:
                page = await _open(
                    _native_history(client, SCOPE, SESSION),
                    SESSION,
                    page_size=2,
                    since=AT,
                    lt=None,
                    token=None,
                )
            else:
                page = await client.beta.sessions.events.list(
                    SESSION,
                    order="asc",
                    limit=2,
                    types=["span.model_request_end"],
                    created_at_gte=AT,
                )
            with pytest.raises(BadRequestError if status == 400 else NotFoundError) as caught:
                await page.get_next_page()
            failures.append((type(caught.value), caught.value.status_code))
            traces.append(requests)
            assert len(requests) == 2
        finally:
            await client.close()
    assert traces[0] == traces[1] and failures[0] == failures[1]


def test_native_query_does_not_serialize_datetimes_before_sdk():
    query = EventQuery(created_at_gte=AT, created_at_lt=AT + timedelta(hours=1))
    fields = query.model_dump(exclude_unset=True)
    assert fields["created_at_gte"] is AT and isinstance(fields["created_at_lt"], datetime)
    assert set(fields) == {"created_at_gte", "created_at_lt"}
    assert EventQuery().model_dump(exclude_unset=True) == {}
    assert EventQuery(created_at_gte=None).model_dump(exclude_unset=True) == {
        "created_at_gte": None
    }


@pytest.mark.parametrize(
    "scope", [None, Scope.platform(reason="test"), Scope.legacy_host_authorized(call_site="test")]
)
async def test_unowned_reconcile_scope_refuses_before_database_or_sdk(scope):
    from daimon.core.usage_reconcile import reconcile_unbilled

    calls = []

    def database():
        calls.append("database")
        raise AssertionError("scope refusal reached database")

    async def record(**kwargs):
        calls.append("record")
        raise AssertionError("scope refusal reached recorder")

    client = build_async_anthropic(transport=httpx.MockTransport(lambda r: calls.append("sdk")))
    try:
        with pytest.raises(ScopeViolation):
            await reconcile_unbilled(
                anthropic=client,
                sessionmaker=database,
                managed_session_id=SESSION,
                record=record,
                scope=scope,
            )
        assert not calls
    finally:
        await client.close()
