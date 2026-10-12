"""Real expired notebook links show browsers an HTML page; other clients keep JSON
unless the operator gives every client the page."""

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from notebook_host.blogs_store import BlogRecord, register_blog
from notebook_host.config import Settings, load_settings
from notebook_host.jail import get_slug_paths
from notebook_host.main import create_app

BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
HTML_ACCEPTS = {"text/html", BROWSER_ACCEPT}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("kind", ["missing", "expired", "wrong-token"])
@pytest.mark.parametrize(
    "accept", ["*/*", "text/html", "application/json", BROWSER_ACCEPT, "text/html;q=0, */*"]
)
async def test_document_refusal_retains_expiry_and_authentication(
    tmp_path: Path, enabled: bool, kind: str, accept: str
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        public_url_base="https://notebooks.invalid/n",
        html_link_errors=enabled,
    )
    app = create_app(settings)
    slug = "private-slug"
    token = "private-token"
    if kind != "missing":
        paths = get_slug_paths(tmp_path, slug)
        paths.notebook.parent.mkdir(parents=True, exist_ok=True)
        paths.notebook.write_text("import marimo as mo\napp = mo.App()\n")
        register_blog(
            settings.resolved_blogs_file,
            BlogRecord(
                slug=slug,
                created_at=1,
                access_token=token,
                expires_at=2 if kind == "expired" else None,
            ),
        )

    def refused_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("an expired or unauthenticated link started a notebook")

    app.state.admin_state.spawner = refused_spawn
    supplied = "wrong-private-token" if kind == "wrong-token" else token
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client:
        response = await client.get(
            f"https://notebooks.invalid/n/{slug}/?access_token={supplied}",
            headers={"Accept": accept},
        )
    assert response.status_code == 404
    assert "set-cookie" not in response.headers
    assert app.state.admin_state.processes == {}
    if enabled or accept in HTML_ACCEPTS:
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert "Ask the sender to share a new link." in response.text
        assert all(value not in response.text for value in (slug, token, supplied, "detail"))
    else:
        assert response.headers["content-type"] == "application/json"
        assert response.content == b'{"detail":"no active notebook: private-slug"}'


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/n/missing/api"),
        ("HEAD", "/n/missing/"),
        ("POST", "/n/missing/"),
        ("PUT", "/n/missing/"),
        ("DELETE", "/n/missing/"),
        ("PATCH", "/n/missing/"),
        ("OPTIONS", "/n/missing/"),
        ("GET", "/admin/notebooks"),
        ("GET", "/unknown"),
    ],
)
@pytest.mark.parametrize("accept", ["*/*", BROWSER_ACCEPT])
async def test_opt_in_changes_no_other_route_or_method(
    tmp_path: Path, method: str, path: str, accept: str
) -> None:
    responses = []
    for enabled in (False, True):
        app = create_app(Settings(_env_file=None, data_dir=tmp_path, html_link_errors=enabled))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), trust_env=False
        ) as client:
            response = await client.request(
                method, "https://notebooks.invalid" + path, headers={"Accept": accept}
            )
        responses.append((response.status_code, response.content, dict(response.headers)))
    assert responses[0] == responses[1]
    assert responses[1][2]["content-type"] == "application/json"


async def test_opt_in_does_not_render_a_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, html_link_errors=True)
    app = create_app(settings)
    register_blog(
        settings.resolved_blogs_file,
        BlogRecord(slug="stopped", created_at=1, access_token="saved-token"),
    )

    async def refused_start(*args: object, **kwargs: object) -> None:
        raise HTTPException(503, "notebook is starting")

    monkeypatch.setattr("notebook_host.proxy.ensure_running", refused_start)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client:
        response = await client.get(
            "https://notebooks.invalid/n/stopped/?access_token=saved-token",
            headers={"Accept": BROWSER_ACCEPT},
        )
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    assert response.content == b'{"detail":"notebook is starting"}'


@pytest.mark.parametrize("enabled", [False, True])
async def test_running_notebook_keeps_the_same_upstream_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, html_link_errors=enabled))
    process = MagicMock()
    process.poll.return_value = None
    state = app.state.admin_state
    state.processes["running"] = state.make_process(
        "running", 8100, process, access_token="saved-token"
    )
    forwarded = []

    def upstream(req: httpx.Request) -> httpx.Response:
        forwarded.append(req)
        return httpx.Response(
            200, content=b"existing notebook", headers={"Content-Type": "text/plain"}
        )

    real_client = httpx.AsyncClient

    def offline_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs.setdefault("transport", httpx.MockTransport(upstream))
        kwargs.setdefault("trust_env", False)
        return real_client(**kwargs)

    monkeypatch.setattr("notebook_host.proxy.httpx.AsyncClient", offline_client)
    async with real_client(transport=httpx.ASGITransport(app=app), trust_env=False) as client:
        response = await client.get("https://notebooks.invalid/n/running/?access_token=saved-token")
    assert response.status_code == 200
    assert response.content == b"existing notebook"
    assert response.headers["content-type"] == "text/plain"
    assert len(forwarded) == 1
    assert str(forwarded[0].url) == "http://localhost:8100/n/running/?access_token=saved-token"


@pytest.mark.parametrize("raw", [None, "false", "true"])
def test_html_errors_for_every_client_require_explicit_environment_opt_in(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    if raw is not None:
        monkeypatch.setenv("DAIMON_NOTEBOOK__HTML_LINK_ERRORS", raw)
    assert load_settings(_env_file=None).html_link_errors is (raw == "true")
