"""Disposable persisted recipients exercise the real report app's refusal boundary."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import HttpUrl, SecretStr
from report_host import reports_store
from report_host.config import Settings
from report_host.main import create_app

BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
HTML_ACCEPTS = {"text/html", BROWSER_ACCEPT}


@pytest.fixture
async def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    conn = reports_store.connect(tmp_path)
    now = datetime.now(UTC)
    reports_store.save_report(
        conn,
        slug="private-report",
        title="private-title",
        tenant_id="private-tenant",
        agent_name="private-agent",
        cap_usd=Decimal("1"),
        agent_token="private-native-token",
        now=now,
    )
    for token, days in (("private-expired", -1), ("private-valid", 1), ("private-revoked", 1)):
        reports_store.add_recipient(
            conn,
            slug="private-report",
            name="private-reader",
            label="reader",
            token=token,
            ttl_days=days,
            now=now,
        )
    reports_store.revoke_recipient(conn, slug="private-report", token="private-revoked", now=now)
    monkeypatch.setattr(reports_store, "connect", lambda _root: conn)

    def refused_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError("public document reads made an upstream HTTP request")

    real_client = httpx.AsyncClient
    outbound_clients = []

    def offline_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs.setdefault("trust_env", False)
        kwargs.setdefault("transport", httpx.MockTransport(refused_http))
        client = real_client(**kwargs)
        outbound_clients.append(client)
        return client

    monkeypatch.setattr("report_host.main.httpx.AsyncClient", offline_client)
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            admin_secrets=[SecretStr("private-admin")],
            mcp_url=HttpUrl("https://mcp.invalid/mcp"),
            public_url_base=HttpUrl("https://reports.invalid"),
        )
    )
    try:
        yield app
    finally:
        for client in outbound_clients:
            await client.aclose()
        conn.close()


async def request(
    app: FastAPI, path: str, *, accept: str = "*/*", method: str = "GET"
) -> httpx.Response:
    # Direct ASGI transport: no client networking, environment or seam dispatch.
    req = httpx.Request(method, "https://reports.invalid" + path, headers={"Accept": accept})
    transport = httpx.ASGITransport(app=app)
    response = await transport.handle_async_request(req)
    await response.aread()
    await transport.aclose()
    return response


@pytest.mark.parametrize("kind", ["missing", "absent-token", "unknown", "expired", "revoked"])
@pytest.mark.parametrize(
    "accept", ["*/*", "text/html", "application/json", BROWSER_ACCEPT, "text/html;q=0, */*"]
)
async def test_document_refusal_is_indistinguishable_and_keeps_403(
    host: FastAPI, kind: str, accept: str
) -> None:
    path = {
        "missing": "/r/missing",
        "absent-token": "/r/private-report",
        "unknown": "/r/private-report?k=private-unknown",
        "expired": "/r/private-report?k=private-expired",
        "revoked": "/r/private-report?k=private-revoked",
    }[kind]
    response = await request(host, path, accept=accept)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    if accept not in HTML_ACCEPTS:
        assert response.headers["content-type"] == "application/json"
        assert response.content == b'{"detail":"this link is not valid for this report"}'
        return
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "Ask the sender to share a new link." in response.text
    assert all(
        value not in response.text
        for value in ("private", "missing", "detail", "expired", "revoked")
    )
    other_refusal = await request(host, "/r/private-report?k=private-unknown", accept=accept)
    assert response.content == other_refusal.content


@pytest.mark.parametrize("accept", ["*/*", "text/html", "application/json", BROWSER_ACCEPT])
async def test_a_valid_link_gets_the_viewer_and_its_cookie(host: FastAPI, accept: str) -> None:
    response = await request(host, "/r/private-report?k=private-valid", accept=accept)
    assert response.status_code == 200
    assert "Link unavailable" not in response.text
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/private-report/state?k=private-expired"),
        ("GET", "/files/private-report/file.pdf?k=private-expired"),
        ("POST", "/api/private-report/ask?k=private-expired"),
        ("GET", "/admin/reports"),
        ("DELETE", "/admin/reports/private-report"),
        ("GET", "/unknown"),
        ("HEAD", "/r/private-report?k=private-expired"),
        ("POST", "/r/private-report?k=private-expired"),
    ],
)
@pytest.mark.parametrize("accept", ["*/*", BROWSER_ACCEPT])
async def test_no_api_file_admin_or_other_method_gets_the_page(
    host: FastAPI, method: str, path: str, accept: str
) -> None:
    response = await request(host, path, method=method, accept=accept)
    assert response.headers["content-type"] == "application/json"
