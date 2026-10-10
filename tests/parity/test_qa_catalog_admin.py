"""The catalog admin callback routes each frozen admin source to its real binding."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from daimon.testing.admin_bindings import AdminBindings, AdminReceipt
from daimon.testing.catalog_admin import MCP_TOKEN_CALL, CatalogAdmin
from daimon.testing.headless_bindings import (
    QA_TOKEN_MCP_URL,
    DbCheckInvalid,
    db_check,
    loopback_transport,
)
from daimon.testing.qa_mcp_host import build_host, cleanup, serve
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Verbatim from the frozen catalog.
NEW27_STEP: dict[str, Any] = {
    "do": "admin",
    "tool": "headless",
    "args": {
        "call": "credential_submit.mcp_token",
        "token": "wrong-token-123",
        "url": "{env.qa_token_mcp_url}",
    },
}
NEW27_SQL = "select used_at is null from credential_requests where id = '{headless.request_id}'"
D7_STEP: dict[str, Any] = {
    "do": "admin",
    "tool": "cli",
    "args": "daimon channels skills add discord {guild_id} {channel_id} {env.qa_library_skill}",
}
ENV = {"env.qa_token_mcp_url": QA_TOKEN_MCP_URL}


@pytest.fixture
async def mcp_transport(tmp_path: Path) -> AsyncIterator[httpx.AsyncBaseTransport]:
    database_url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    assert database_url, "the parity lane provides an isolated test database"
    qa = await build_host(database_url=database_url, port=0, root=tmp_path)
    try:
        async with serve(qa) as url:
            yield loopback_transport(url)
    finally:
        await qa.engine.dispose()
        await cleanup(qa.manifest.path(tmp_path), database_url=database_url)


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_new27_runs_through_the_admin_callback(
    backend: Any,
    mcp_transport: httpx.AsyncBaseTransport,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = CatalogAdmin(backend, db_session_factory, mcp_transport=mcp_transport)
    receipt = await admin(NEW27_STEP, ENV)

    assert (receipt.status, receipt.operation, receipt.code) == ("executed", MCP_TOKEN_CALL, None)
    assert receipt.observations["outcome"] == "token_rejected"
    assert receipt.observations["probe_statuses"] == [401]
    assert receipt.observations["used"] is True
    # The cell's own assertion then runs on the values the step produced.
    with pytest.raises(DbCheckInvalid, match="SQLSTATE 42703"):
        await db_check(db_session_factory, NEW27_SQL, receipt.values)


@pytest.mark.parametrize(
    ("step", "values", "code"),
    [
        ({**NEW27_STEP, "args": {**NEW27_STEP["args"], "call": "other"}}, ENV, "HEADLESS_CALL"),
        (NEW27_STEP, {}, "ADMIN_ARGUMENTS_UNBOUND"),
        ({**NEW27_STEP, "args": {**NEW27_STEP["args"], "extra": "x"}}, ENV, "ARGUMENTS"),
        ({**NEW27_STEP, "allow_fail": True}, ENV, "ADMIN_SOURCE_UNSUPPORTED"),
        (D7_STEP, ENV, "ADMIN_PRODUCT_BINDINGS_UNBOUND"),
    ],
)
async def test_an_unbindable_source_stays_typed_pending_with_no_effect(
    step: dict[str, Any], values: dict[str, str], code: str
) -> None:
    class _NeverOpened(async_sessionmaker[AsyncSession]):
        def __call__(self, **local_kw: object) -> AsyncSession:  # type: ignore[override]
            raise AssertionError("a pending admin source opened a session")

    def no_egress(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"egress attempted: {request.url}")

    admin = CatalogAdmin("openai", _NeverOpened(), mcp_transport=httpx.MockTransport(no_egress))
    receipt = await admin(step, values)
    assert receipt.status == "pending"
    assert receipt.code is not None and code in receipt.code


async def test_every_other_source_goes_to_the_product_admin_bindings() -> None:
    seen: list[tuple[Mapping[str, Any], Mapping[str, str]]] = []

    class _Product:
        async def execute(
            self, source: Mapping[str, Any], values: Mapping[str, str]
        ) -> AdminReceipt:
            seen.append((source, values))
            return AdminReceipt(status="pending", backend="openai", operation="channel_skills_add")

    admin = CatalogAdmin(
        "openai",
        cast(async_sessionmaker[AsyncSession], None),
        product=cast(AdminBindings, _Product()),
    )
    receipt = await admin(D7_STEP, ENV)
    assert seen == [(D7_STEP, ENV)]
    assert receipt.operation == "channel_skills_add"
