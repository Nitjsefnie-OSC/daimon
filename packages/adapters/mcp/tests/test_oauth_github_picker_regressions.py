"""Regressions found while exercising the real multi-owner GitHub picker end to end.

The browser tests need Playwright and the Chromium build under
~/.cache/ms-playwright; they skip when either is missing. Run them with
`uv run --with playwright==1.58.0 pytest <this file> -n 0`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from daimon.adapters.mcp import oauth_github

# The fixture is defined next to the route tests; importing it registers it here.
from .test_oauth_github_connect import (  # noqa: F401
    _EditorEnv,
    editor_env,
)

_CHROME = Path(
    os.environ.get(
        "CHROME", Path.home() / ".cache/ms-playwright/chromium-1248/chrome-linux64/chrome"
    )
)


@pytest.mark.asyncio
async def test_setup_redirect_without_state_resumes_the_flow_from_the_cookie(
    editor_env: _EditorEnv,  # noqa: F811
) -> None:
    """GitHub's setup redirect omits `state` after an update or an owner's approval.

    People land on `/oauth/github/setup?installation_id=..&setup_action=..` with no
    `state` after changing an installation from the picker, and used to get "This
    link has expired." The browser still holds the flow cookies, so the page carries
    on to the picker, which re-lists installations as the user anyway.
    """
    env = editor_env
    env.multi_owners = True
    state, _hidden = await env.open_editor()

    with_state = await env.browser.get(
        "/oauth/github/setup",
        params={"installation_id": "78", "setup_action": "install", "state": state},
    )
    assert with_state.status_code in (302, 307)

    without_state = await env.browser.get(
        "/oauth/github/setup", params={"installation_id": "78", "setup_action": "update"}
    )
    assert without_state.status_code in (302, 307), without_state.text
    location = urlparse(without_state.headers["location"])
    assert location.path == "/oauth/github/confirm"
    assert parse_qs(location.query)["state"] == [state]


def _editor_html() -> str:
    def owner(
        inst: int, owner_id: int, login: str, kind: str, repos: int
    ) -> oauth_github._Installation:
        return oauth_github._Installation(
            id=inst,
            owner_id=owner_id,
            owner_login=login,
            repository_selection="selected",
            owner_type=kind,
            repos=tuple(
                oauth_github._Repo(inst * 100 + n, owner_id, inst, f"{login}/repo-{n}", "admin")
                for n in range(repos)
            ),
        )

    page = oauth_github._agent_editor_page(
        root="https://mcp.test",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        agent_name="Editor",
        workspace="Test",
        platform="discord",
        grants=[],
        installations=[
            owner(1, 1, "sam", "User", 2),
            owner(2, 2, "northwind", "Organization", 2),
            owner(3, 3, "emptyorg", "Organization", 0),
        ],
        snapshot="snap",
        cancel_url="https://mcp.test/cancel",
        install_url="https://github.com/apps/x/installations/new",
        github_user_id=1,
    )
    return page.body.decode()


@pytest.fixture
def browser_page() -> Iterator[object]:
    sync_api = pytest.importorskip("playwright.sync_api")
    if not _CHROME.exists():
        pytest.skip("Chromium for Playwright is not installed")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(_CHROME), args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content(_editor_html())
        yield page
        browser.close()


def test_owner_list_title_resets_when_the_picker_reopens(browser_page: object) -> None:
    """After picking an owner and closing, the owner list must not say "Add northwind repos"."""
    page = browser_page  # type: ignore[assignment]
    page.click("#open-repo-picker")
    page.click('.gh-owner-button[data-owner="northwind"]')
    assert page.inner_text("#repo-picker-title") == "Add northwind repos"
    page.click("#close-repo-picker")
    page.click("#open-repo-picker")
    assert page.is_visible("#repo-owner-choice")
    assert page.inner_text("#repo-picker-title") == "Add repos"


def test_owner_with_nothing_to_add_says_so(browser_page: object) -> None:
    """An installed owner with no repos opens an empty sheet ("Showing 0 of 0") with no words."""
    page = browser_page  # type: ignore[assignment]
    page.click("#open-repo-picker")
    page.click('.gh-owner-button[data-owner="emptyorg"]')
    sheet = page.inner_text("#repo-owner-sheet").lower()
    assert "no repos" in sheet


@pytest.mark.asyncio
async def test_setup_without_state_still_needs_the_matching_flow_cookie(
    editor_env: _EditorEnv,  # noqa: F811
) -> None:
    """The remembered state only helps the browser that holds the flow cookie too."""
    env = editor_env
    env.multi_owners = True
    state, _hidden = await env.open_editor()
    params = {"installation_id": "78", "setup_action": "update"}

    flow_cookie = env.browser.cookies.get(oauth_github._COOKIE)
    env.browser.cookies.delete(oauth_github._COOKIE)
    assert (await env.browser.get("/oauth/github/setup", params=params)).status_code == 400

    env.browser.cookies.set(oauth_github._COOKIE, "someone-else")
    assert (await env.browser.get("/oauth/github/setup", params=params)).status_code == 400

    assert flow_cookie is not None
    env.browser.cookies.set(oauth_github._COOKIE, flow_cookie)
    env.browser.cookies.delete(oauth_github._STATE_COOKIE)
    assert (await env.browser.get("/oauth/github/setup", params=params)).status_code == 400

    # A state in the query wins over the remembered one and must still be the flow's.
    wrong = await env.browser.get("/oauth/github/setup", params={**params, "state": "nope"})
    assert wrong.status_code == 400
    right = await env.browser.get("/oauth/github/setup", params={**params, "state": state})
    assert parse_qs(urlparse(right.headers["location"]).query)["state"] == [state]


def test_selected_view_is_not_titled_with_one_owner(browser_page: object) -> None:
    """ "Selected (n)" lists picks from every owner, so the title can't name just one."""
    page = browser_page  # type: ignore[assignment]
    page.click("#open-repo-picker")
    page.click('.gh-owner-button[data-owner="sam"]')
    page.check('.gh-picker-choice[data-owner="sam"] .gh-picker-toggle >> nth=0')
    page.click("#back-repo-owners")
    page.click('.gh-owner-button[data-owner="northwind"]')
    page.check('.gh-picker-choice[data-owner="northwind"] .gh-picker-toggle >> nth=0')
    page.click("#show-selected-repos")
    assert page.inner_text("#repo-picker-title") == "Selected repos"
    page.click("#show-selected-repos")
    assert page.inner_text("#repo-picker-title") == "Add northwind repos"
