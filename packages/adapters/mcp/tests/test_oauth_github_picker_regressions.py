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
