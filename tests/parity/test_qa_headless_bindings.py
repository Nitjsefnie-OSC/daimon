"""The headless bindings drive real product paths and report what happened.

QA-NEW27-MISTYPED-TOKEN-HEADLESS: a token the MCP server rejects, submitted
through the real credential modal, against the real token-gated loopback QA
host. The frozen `db_check` is then run exactly as the catalog states it.
QA-I7-MCP-OAUTH-DISCOVERY and QA-NEW24-EXPIRED-LINK-HTML-PAGE: the frozen
`http_check`s against the real MCP, notebook and report apps on loopback.

The bindings report; they do not judge. Where these tests pin a verdict, it
is today's evidence for the cell, and a fix to the product flips it here.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from daimon.core.mcp_token_check import is_token_rejected
from daimon.testing.headless_bindings import (
    QA_TOKEN_MCP_URL,
    DbCheckInvalid,
    bind_placeholders,
    db_check,
    expand_url,
    http_check,
    http_check_holds,
    loopback_transport,
    serve_catalog_hosts,
    submit_mcp_token,
)
from daimon.testing.qa_mcp_host import QaHost, build_host, cleanup, serve
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Verbatim from the frozen catalog scenarios.
I7 = (
    {
        "kind": "http_check",
        "url": "{env.mcp_public_url}/.well-known/oauth-protected-resource/mcp",
        "expect_status": 200,
    },
    {
        "kind": "http_check",
        "url": "{env.mcp_public_url}/.well-known/oauth-protected-resource/mcp/",
        "expect_status": 200,
    },
)
NEW24 = (
    {
        "kind": "http_check",
        "url": "{env.notebook_host}/qa-missing-{nonce}/",
        "expect_content_type": "text/html",
        "body_absent": '\\{\\"detail\\"',
    },
    {
        "kind": "http_check",
        "url": "{env.report_host}/r/qa-missing-{nonce}",
        "expect_content_type": "text/html",
        "body_absent": '\\{\\"detail\\"',
    },
)
NEW27_TOKEN = "wrong-token-123"
NEW27_SQL = "select used_at is null from credential_requests where id = '{headless.request_id}'"


@dataclass
class _Served:
    qa: QaHost
    transport: httpx.AsyncBaseTransport


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[_Served]:
    database_url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    assert database_url, "the parity lane provides an isolated test database"
    qa = await build_host(database_url=database_url, port=0, root=tmp_path)
    try:
        async with serve(qa) as url:
            yield _Served(qa, loopback_transport(url))
    finally:
        await qa.engine.dispose()
        await cleanup(qa.manifest.path(tmp_path), database_url=database_url)


async def test_the_qa_host_really_tells_a_wrong_token_from_the_right_one(served: _Served) -> None:
    """The 401 is the server's verdict, not an artifact of the binding."""
    from daimon.core.mcp_oauth.discovery import McpProbe, probe_mcp_server

    async def probe(url: str, token: str) -> McpProbe:
        async with httpx.AsyncClient(transport=served.transport) as http:
            return await probe_mcp_server(http, mcp_server_url=url, bearer_token=token)

    assert await is_token_rejected(probe, mcp_server_url=QA_TOKEN_MCP_URL, token=NEW27_TOKEN)
    assert not await is_token_rejected(
        probe, mcp_server_url=QA_TOKEN_MCP_URL, token=served.qa.bearer.token
    )


async def test_new27_a_rejected_token_through_the_real_modal(
    served: _Served, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    receipt = await submit_mcp_token(
        db_session_factory, mcp_transport=served.transport, token=NEW27_TOKEN
    )

    # The real server refused the token, and nothing reached a vault or the agent.
    assert receipt.probe_statuses == (401,)
    assert receipt.outcome == "token_rejected"
    assert not any(r.startswith("POST") for r in receipt.ma_requests), receipt.ma_requests
    assert any("did not accept" in reply for reply in receipt.replies), receipt.replies
    assert NEW27_TOKEN not in " ".join(receipt.replies)

    assert receipt.used, "the rejected token still spent the request (friction NEW-27)"

    # The frozen check, run exactly as the catalog writes it, cannot execute:
    # `credential_requests` has no `id` column (it is keyed by its secret token).
    # That is a catalog defect, reported as such, never a PASS or a FAIL.
    with pytest.raises(DbCheckInvalid, match="SQLSTATE 42703"):
        await db_check(db_session_factory, NEW27_SQL, receipt.values)
    # Diagnostic only, not the catalog's check: the same fact keyed by the
    # request's UUID identity is False, so a corrected check would FAIL today.
    diagnostic = NEW27_SQL.replace("where id =", "where idempotency_key =")
    assert await db_check(db_session_factory, diagnostic, receipt.values) is False


def test_placeholders_bind_only_known_uuid_values() -> None:
    values = {"headless.request_id": "6f0d3c1e-8a52-4f3b-9d43-2a3c1d9e7b10"}
    assert bind_placeholders(NEW27_SQL, values).endswith("'6f0d3c1e-8a52-4f3b-9d43-2a3c1d9e7b10'")
    with pytest.raises(ValueError, match="unbound placeholder"):
        bind_placeholders(NEW27_SQL, {})
    with pytest.raises(ValueError):
        bind_placeholders(NEW27_SQL, {"headless.request_id": "x' or '1'='1"})


async def test_db_check_runs_exactly_one_read_only_select(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await db_check(db_session_factory, "select 1", {}) == 1
    with pytest.raises(ValueError, match="one SELECT"):
        await db_check(db_session_factory, "select 1; select 2", {})
    with pytest.raises(ValueError, match="one SELECT"):
        await db_check(db_session_factory, "delete from credential_requests", {})
    with pytest.raises(ValueError, match="one value"):
        await db_check(db_session_factory, "select 1, 2", {})


async def test_i7_and_new24_against_the_real_apps(tmp_path: Path) -> None:
    database_url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    assert database_url, "the parity lane provides an isolated test database"
    async with serve_catalog_hosts(database_url, tmp_path) as origins:
        values = {**origins, "nonce": "n2-headless"}
        i7 = [await http_check(str(a["url"]), values) for a in I7]
        new24 = [await http_check(str(a["url"]), values) for a in NEW24]

    # I7: the base MCP app serves no protected-resource metadata at either path
    # (OAuth metadata is served only under hub mounts), so the cell FAILs.
    assert [(r.status, http_check_holds(a, r)) for a, r in zip(I7, i7, strict=True)] == [
        (404, False),
        (404, False),
    ]
    # NEW24: a missing notebook and an invalid report link answer JSON, not an
    # HTML page (friction NEW-24), so the cell FAILs.
    assert [r.content_type for r in new24] == ["application/json", "application/json"]
    assert all('{"detail"' in r.body for r in new24)
    assert [http_check_holds(a, r) for a, r in zip(NEW24, new24, strict=True)] == [False, False]


def test_http_check_semantics_and_url_safety() -> None:
    from daimon.testing.headless_bindings import HttpReceipt

    page = HttpReceipt(url="u", status=200, content_type="text/html; charset=utf-8", body="<p>")
    assert http_check_holds(NEW24[0], page)
    assert http_check_holds(I7[0], page)
    json_page = HttpReceipt(url="u", status=200, content_type="text/html", body='{"detail": 1}')
    assert not http_check_holds(NEW24[0], json_page)
    assert not http_check_holds(I7[0], HttpReceipt(url="u", status=404, content_type=None, body=""))
    origin = {"env.notebook_host": "http://127.0.0.1:9", "nonce": "abc"}
    assert expand_url(str(NEW24[0]["url"]), origin) == "http://127.0.0.1:9/qa-missing-abc/"
    with pytest.raises(ValueError, match="loopback only"):
        expand_url(str(NEW24[0]["url"]), {**origin, "env.notebook_host": "https://example.com"})
    with pytest.raises(ValueError, match="unsafe value"):
        expand_url(str(NEW24[0]["url"]), {**origin, "nonce": "../admin"})
    with pytest.raises(ValueError, match="unbound placeholder"):
        expand_url(str(NEW24[0]["url"]), {"env.notebook_host": "http://127.0.0.1:9"})


# Safety boundaries: refusals happen before any connection or client exists.


class _NeverOpened(async_sessionmaker[AsyncSession]):
    """A factory that fails the test if a binding ever opens a session from it."""

    def __call__(self, **local_kw: object) -> AsyncSession:  # type: ignore[override]
        raise AssertionError("a refused binding opened a session")


def _factory_for(url: str, connects: list[object]) -> async_sessionmaker[AsyncSession]:
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    event.listen(engine.sync_engine, "connect", lambda *_: connects.append(url))
    return _NeverOpened(bind=engine)


@pytest.mark.parametrize(
    "url",
    [
        # Never connected to: the factories below are refused before any session.
        "postgresql+asyncpg://daimon:daimon@localhost:5432/daimon",
        "postgresql+asyncpg://daimon:daimon@localhost:5432/daimon_test_nc_other",
        "postgresql+asyncpg://daimon:daimon@db.example.com:5432/daimon_test_nc_n2",
        "postgresql+asyncpg://daimon:daimon@localhost:5432/qa?host=db.example.com",
    ],
)
async def test_a_binding_refuses_a_database_it_does_not_own_before_connecting(url: str) -> None:
    from daimon.testing.headless_bindings import BindingRefused

    connects: list[object] = []
    factory = _factory_for(url, connects)
    with pytest.raises(BindingRefused):
        await submit_mcp_token(
            factory, mcp_transport=httpx.MockTransport(_no_egress), token=NEW27_TOKEN
        )
    with pytest.raises(BindingRefused):
        await db_check(factory, "select 1", {})
    with pytest.raises(BindingRefused):
        async with serve_catalog_hosts(url, Path("/nonexistent")):
            pass
    assert connects == []


async def test_a_binding_refuses_a_factory_without_one_explicit_bind() -> None:
    from daimon.testing.headless_bindings import BindingRefused

    with pytest.raises(BindingRefused, match="explicit"):
        await db_check(_NeverOpened(), "select 1", {})
    with pytest.raises(BindingRefused, match="binds"):
        await db_check(_NeverOpened(binds={object: object()}), "select 1", {})


def _no_egress(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"egress attempted: {request.url}")


@pytest.mark.parametrize(
    "template",
    [
        "https://external.invalid/secret",
        "https://external.invalid/?next={env.notebook_host}",
        "http://user:pw@127.0.0.1:9/x",
        "http://127.0.0.1:10/qa-missing-{nonce}/",
        "http://localhost:9/x",
    ],
)
async def test_http_check_refuses_any_final_url_off_the_served_origins(
    template: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.testing import headless_bindings
    from daimon.testing.headless_bindings import BindingRefused

    def no_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a client was built for a refused URL")

    monkeypatch.setattr(headless_bindings.httpx, "AsyncClient", no_client)
    with pytest.raises(BindingRefused):
        await http_check(template, {"env.notebook_host": "http://127.0.0.1:9", "nonce": "abc"})


def test_the_probe_transport_maps_to_loopback_only() -> None:
    from daimon.testing.headless_bindings import BindingRefused

    for url in ("https://127.0.0.1:9/mcp", "http://10.0.0.1:9/mcp", "http://127.0.0.1/mcp"):
        with pytest.raises(BindingRefused):
            loopback_transport(url)
