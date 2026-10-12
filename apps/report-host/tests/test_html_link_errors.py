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
from report_host.config import Settings, load_settings
from report_host.main import create_app

BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
HTML_ACCEPTS = {"text/html", BROWSER_ACCEPT}


@pytest.fixture
async def host_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[FastAPI, FastAPI]]:
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
    apps = tuple(
        create_app(
            Settings(
                _env_file=None,
                data_dir=tmp_path,
                admin_secrets=[SecretStr("private-admin")],
                mcp_url=HttpUrl("https://mcp.invalid/mcp"),
                public_url_base=HttpUrl("https://reports.invalid"),
                html_link_errors=enabled,
            )
        )
        for enabled in (False, True)
    )
    try:
        yield apps
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
    host_pair: tuple[FastAPI, FastAPI], kind: str, accept: str
) -> None:
    path = {
        "missing": "/r/missing",
        "absent-token": "/r/private-report",
        "unknown": "/r/private-report?k=private-unknown",
        "expired": "/r/private-report?k=private-expired",
        "revoked": "/r/private-report?k=private-revoked",
    }[kind]
    default = await request(host_pair[0], path, accept=accept)
    opted = await request(host_pair[1], path, accept=accept)
    assert default.status_code == opted.status_code == 403
    if accept in HTML_ACCEPTS:
        assert default.content == opted.content, "a browser gets the page without the opt-in"
    else:
        assert default.headers["content-type"] == "application/json"
        assert default.content == b'{"detail":"this link is not valid for this report"}'
    assert opted.headers["content-type"] == "text/html; charset=utf-8"
    assert "Ask the sender to share a new link." in opted.text
    assert "set-cookie" not in opted.headers
    assert all(
        value not in opted.text for value in ("private", "missing", "detail", "expired", "revoked")
    )
    other_refusal = await request(host_pair[1], "/r/private-report?k=private-unknown")
    assert opted.content == other_refusal.content


@pytest.mark.parametrize("accept", ["*/*", "text/html", "application/json", BROWSER_ACCEPT])
async def test_valid_viewer_bytes_and_cookie_are_unchanged(
    host_pair: tuple[FastAPI, FastAPI], accept: str
) -> None:
    responses = [
        await request(app, "/r/private-report?k=private-valid", accept=accept) for app in host_pair
    ]
    assert responses[0].status_code == responses[1].status_code == 200
    assert responses[0].content == responses[1].content
    assert dict(responses[0].headers) == dict(responses[1].headers)
    assert "httponly" in responses[1].headers["set-cookie"].lower()


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
async def test_opt_in_changes_no_api_file_admin_or_other_method(
    host_pair: tuple[FastAPI, FastAPI], method: str, path: str, accept: str
) -> None:
    responses = [await request(app, path, method=method, accept=accept) for app in host_pair]
    assert responses[0].status_code == responses[1].status_code
    assert responses[0].content == responses[1].content
    assert dict(responses[0].headers) == dict(responses[1].headers)
    assert responses[1].headers["content-type"] == "application/json"


@pytest.mark.parametrize("raw", [None, "false", "true"])
def test_html_errors_for_every_client_require_explicit_environment_opt_in(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    monkeypatch.delenv("DAIMON_REPORT__HTML_LINK_ERRORS", raising=False)
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "offline-admin")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "https://mcp.invalid/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "https://reports.invalid")
    if raw is not None:
        monkeypatch.setenv("DAIMON_REPORT__HTML_LINK_ERRORS", raw)
    assert load_settings(_env_file=None).html_link_errors is (raw == "true")
