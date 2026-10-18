"""Browser checks for the real server-rendered multi-owner agent picker.

Run: uv run --with playwright==1.58.0 python scripts/check_github_picker.py
Uses synthetic fixture data and the built web.css; no GitHub login or test DB.
"""

from __future__ import annotations

import base64
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from daimon.adapters.mcp.oauth_github import (
    _agent_editor_page,
    _Installation,
    _Repo,
    _saved_agent_page,
)
from daimon.core.stores.github_access import AgentRepo
from playwright.sync_api import Page, Route, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "packages/adapters/mcp/daimon/adapters/mcp/static/web.css"
MARK = ROOT / "packages/adapters/mcp/daimon/adapters/mcp/static/daimon-face.png"
CHROME = os.environ.get(
    "CHROME", str(Path.home() / ".cache/ms-playwright/chromium-1248/chrome-linux64/chrome")
)
SHOTS = Path("/home/derick/scratch/daimon-os-ux/multi-org/picker-shots")


def grant(repo_id: int, name: str, *, working: bool = False) -> AgentRepo:
    now = datetime.now(UTC)
    return AgentRepo(
        repo_id=repo_id,
        full_name=name,
        scope="agent",
        max_access="write",
        baseline_access="write",
        ceiling_access="write",
        staged=False,
        status="active",
        added_by_account_id=None,
        added_at=now,
        granted_by_account_id=None,
        granted_at=now,
        is_working_repo=working,
    )


def inline_assets(page_html: str) -> str:
    mark = base64.b64encode(MARK.read_bytes()).decode()
    page_html = page_html.replace("/web/daimon-face.png", f"data:image/png;base64,{mark}")
    # Exercise the image markup and shapes without a network request in tests.
    page_html = re.sub(
        r'https://avatars\.githubusercontent\.com/[^" ]+',
        f"data:image/png;base64,{mark}",
        page_html,
    )
    css = CSS.read_text()
    for weight in ("Regular", "SemiBold", "Bold"):
        font = CSS.with_name(f"Inter-{weight}.ttf")
        encoded = base64.b64encode(font.read_bytes()).decode()
        for url in (f"url('./Inter-{weight}.ttf')", f"url(./Inter-{weight}.ttf)"):
            css = css.replace(url, f"url(data:font/ttf;base64,{encoded})")
    return re.sub(
        r'<link rel="stylesheet" href="[^"]+">',
        lambda _: f"<style>{css}</style>",
        page_html,
        count=1,
    )


def fixture_page(
    *,
    one_owner: bool = False,
    error_owner: bool = False,
    stale: bool = False,
    three_granted: bool = False,
) -> str:
    owners = (
        (77, 55, "sam-rivera", "User"),
        (78, 56, "northwind-labs", "Organization"),
        (79, 57, "tidewater-eap", "Organization"),
    )
    if one_owner:
        owners = owners[:1]
    installations = [
        _Installation(
            id=installation_id,
            owner_id=owner_id,
            owner_login=login,
            repository_selection="selected",
            owner_type=owner_type,
            avatar_url=(
                f"https://avatars.githubusercontent.com/u/{owner_id}"
                if installation_id in (77, 78)
                else None
            ),
            fetch_state="error" if error_owner and installation_id == 79 else "ok",
            repos=tuple(
                _Repo(repo_id, owner_id, installation_id, f"{login}/{name}", "admin")
                for repo_id, name in (
                    (installation_id * 100 + 1, "saved"),
                    (installation_id * 100 + 2, "new"),
                )
            )
            if not (error_owner and installation_id == 79)
            else (),
        )
        for installation_id, owner_id, login, owner_type in owners
    ]
    grants = (
        [
            grant(7801, "northwind-labs/saved", working=True),
            grant(7701, "sam-rivera/saved"),
        ]
        if not one_owner
        else [grant(7701, "sam-rivera/saved", working=True)]
    )
    if stale:
        grants.append(grant(9901, "lost-org/old"))
    if three_granted:
        grants.append(grant(7901, "tidewater-eap/saved"))
    response = _agent_editor_page(
        root="https://mcp.test",
        state="fixture",
        invitation_hash="fixture",
        secret="fixture",
        agent_name="Atlas",
        workspace="Fixture",
        platform="discord",
        grants=grants,
        installations=installations,
        snapshot="fixture",
        cancel_url="https://mcp.test/cancel",
        install_url="https://github.com/apps/daimon/installations/new",
        github_user_id=55,
    )
    return inline_assets(response.body.decode())


def saved_page() -> str:
    grants = [
        grant(7801, "northwind-labs/saved", working=True),
        grant(7802, "northwind-labs/new"),
        grant(7901, "tidewater-eap/saved"),
        grant(7902, "tidewater-eap/new"),
    ]
    response = _saved_agent_page("Atlas", grants, {repo.repo_id for repo in grants})
    return inline_assets(response.body.decode())


ROW_MEASURE = """() => {
  const rect = el => el.getBoundingClientRect();
  const center = el => (rect(el).top + rect(el).bottom) / 2;
  const firstLine = el => {
    const range = document.createRange();
    range.selectNodeContents(el.firstChild || el);
    const box = [...range.getClientRects()].find(item => item.width > 0);
    return box ? (box.top + box.bottom) / 2 : center(el);
  };
  const rows = [...document.querySelectorAll(
    '.gh-agent-row:not([hidden]), .gh-picker-choice:not([hidden]), .gh-saved-row'
  )].filter(el => rect(el).width && !el.closest('template'));
  return rows.map(row => {
    const name = row.querySelector('.gh-row-name, .gh-picker-name, .gh-saved-name') || row;
    const icon = row.querySelector('.gh-row-icon .web-icon, .gh-row-icon');
    const action = row.querySelector('.gh-row-action:not([hidden]), input[type=checkbox]');
    const style = getComputedStyle(name);
    return {text: name.textContent.trim().slice(0, 30), height: rect(row).height,
      hasNote: !!row.querySelector('.gh-repo-locked, .gh-repo-state'),
      lines: Math.round(rect(name).height / parseFloat(style.lineHeight)),
      iconDy: icon ? center(icon) - firstLine(name) : null,
      actionDy: action ? center(action) - firstLine(name) : null,
      weight: style.fontWeight, color: style.color, size: style.fontSize};
  });
}"""


def check_rows(page: Page) -> int:
    rows = page.evaluate(ROW_MEASURE)
    assert rows, "No rows measured"
    for row in rows:
        assert row["height"] >= 56, row
        if row["lines"] == 1 and not row["hasNote"]:
            assert abs(row["height"] - 56) <= 1, row
        elif row["lines"] > 1 and not row["hasNote"]:
            assert abs(row["height"] - (56 + (row["lines"] - 1) * 24)) <= 1, row
        for key in ("iconDy", "actionDy"):
            if row[key] is not None:
                assert abs(row[key]) <= 1, (key, row)
        assert row["weight"] == "400", row
        assert row["color"] == "rgb(28, 27, 24)", row
        assert row["size"] == "16px", row
    anatomy = page.evaluate(
        """() => {
          const rect = el => el.getBoundingClientRect();
          const cx = el => (rect(el).left + rect(el).right) / 2;
          const cy = el => (rect(el).top + rect(el).bottom) / 2;
          const heads = [...document.querySelectorAll('.gh-owner-heading')]
            .filter(el => rect(el).width).map(head => {
              const avatar = head.querySelector('.gh-owner-avatar, .gh-owner-avatar-fallback');
              const icon = head.parentElement.querySelector('.gh-row-icon');
              return {height: rect(head).height,
                column: avatar && icon ? cx(avatar) - cx(icon) : null};
            });
          const card = document.querySelector('#working-card:not([hidden])');
          const name = card?.querySelector('#working-card-name');
          const button = card?.querySelector('#change-working');
          const pin = card?.querySelector('.web-icon');
          let cardData = null;
          if (card && name) {
            const lines = Math.round(rect(name).height / 24);
            const first = document.createRange();
            first.selectNodeContents(name.firstElementChild || name);
            const line = [...first.getClientRects()].find(r => r.width);
            const firstCy = line ? (line.top + line.bottom) / 2 : cy(name);
            cardData = {height: rect(card).height, lines,
              iconDy: pin ? cy(pin) - firstCy : null,
              actionDy: button ? cy(button) - firstCy : null,
              ownerMuted: !!name.querySelector('.gh-working-owner') &&
                getComputedStyle(name.querySelector('.gh-working-owner')).color !==
                getComputedStyle(name).color,
              text: name.innerText};
          }
          return {heads, card: cardData};
        }"""
    )
    for head in anatomy["heads"]:
        assert head["height"] == 40, head
        if head["column"] is not None:
            assert abs(head["column"]) <= 1, head
    card = anatomy["card"]
    if card and "Hidden repo" not in card["text"]:
        assert abs(card["height"] - (56 + (card["lines"] - 1) * 24)) <= 1, card
        assert card["ownerMuted"] and "/" in card["text"], card
        assert abs(card["iconDy"]) <= 1, card
        assert abs(card["actionDy"]) <= 1, card
    return len(rows)


def capture(width: int) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        page.set_content(fixture_page(three_granted=True))
        font_loaded = page.evaluate(
            "() => document.fonts.ready.then(() => document.fonts.check('16px Inter'))"
        )
        assert font_loaded

        def shot(name: str) -> None:
            page.screenshot(path=str(SHOTS / f"{name}-{width}.png"), full_page=True)

        assert page.locator("#connected-repos .gh-owner-group:visible").count() == 3
        check_rows(page)
        shot("editor")

        page.get_by_role("button", name="Add repos", exact=True).click()
        shot("owner-choice")
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        check_rows(page)
        shot("owner-sheet")
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').check()
        page.locator("#stage-repos").click()
        assert page.locator(".gh-agent-row.gh-added-row").count() == 1
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.get_by_role("button", name="Back").click()
        assert page.locator("#owner-picker-count").inner_text() == "1 selected"
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7902"] input').check()
        page.locator("#show-selected-repos").click()
        assert page.locator(".gh-selected-owner").count() == 2
        check_rows(page)
        shot("selected")
        page.locator("#stage-repos").click()
        assert page.locator(".gh-agent-row.gh-added-row").count() == 2
        page.locator('.gh-agent-row[data-repo-id="7701"] .gh-remove-action').click()
        check_rows(page)
        shot("unsaved")

        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.locator("#close-repo-picker").click()
        assert page.locator(".gh-agent-row.gh-added-row").count() == 2
        assert sorted(
            page.locator("#github-connect-form").evaluate(
                "form => new FormData(form).getAll('repo')"
            )
        ) == ["7802", "7902"]
        assert page.locator("#github-connect-form").evaluate(
            "form => [...new FormData(form).keys()].every(key => !key.startsWith('access_'))"
        )
        assert page.locator("#github-connect-form").evaluate(
            "form => new FormData(form).getAll('remove')"
        ) == ["7701"]

        def save(route: Route) -> None:
            route.fulfill(status=200, content_type="text/html", body=saved_page())

        page.route("https://mcp.test/oauth/github/confirm", save)
        page.locator("#save-repos").click()
        expect(page.get_by_text("Atlas can read and change these 4 repos.")).to_be_visible()
        assert page.locator(".gh-saved-groups .gh-owner-group").count() == 2
        check_rows(page)
        shot("saved")
        browser.close()


def check_staged_edits(width: int) -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")

        def fresh() -> None:
            page.set_content(fixture_page())

        def add_northwind() -> None:
            page.get_by_role("button", name="Add repos", exact=True).click()
            page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
            page.locator('.gh-picker-choice[data-repo-id="7802"] input').check()
            page.locator("#stage-repos").click()
            assert page.locator('.gh-agent-row[data-repo-id="7802"].gh-added-row').count() == 1

        def posted_repos() -> list[str]:
            return page.locator("#github-connect-form").evaluate(
                "form => new FormData(form).getAll('repo')"
            )

        fresh()
        add_northwind()
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').uncheck()
        assert posted_repos() == []
        assert page.locator('.gh-agent-row[data-repo-id="7802"]').count() == 0
        page.get_by_role("button", name="Back").click()
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7902"] input').check()
        page.get_by_role("button", name="Back").click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert not page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.locator("#stage-repos").click()
        assert posted_repos() == ["7902"]

        fresh()
        add_northwind()
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator("#show-selected-repos").click()
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').uncheck()
        assert posted_repos() == []
        assert page.locator("#show-selected-repos").get_attribute("aria-pressed") == "false"
        page.locator("#close-repo-picker").click()

        fresh()
        add_northwind()
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator("#owner-clear-selection").click()
        assert posted_repos() == []
        assert page.locator("#selection-status").inner_text() == "No changes to save"
        assert page.locator(".gh-editor-footer").is_hidden()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert not page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.locator("#close-repo-picker").click()

        fresh()
        add_northwind()
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        page.locator("#show-selected-repos").click()
        page.locator("#clear-selection").click()
        assert posted_repos() == []
        page.locator("#close-repo-picker").click()

        fresh()
        add_northwind()
        page.locator("#discard-repos").click()
        assert posted_repos() == []
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert not page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        page.locator("#close-repo-picker").click()

        fresh()
        add_northwind()
        page.locator('.gh-agent-row[data-repo-id="7802"] .gh-remove-action').click()
        assert posted_repos() == []
        page.get_by_role("button", name="Add repos", exact=True).click()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        assert not page.locator('.gh-picker-choice[data-repo-id="7802"] input').is_checked()
        browser.close()
    return 6


def check_no_js() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": 390, "height": 900}, java_script_enabled=False)
        page.set_content(fixture_page(one_owner=True))
        choice = page.locator('noscript input[name="repo"]').first
        expect(choice).to_be_visible()
        choice.check()
        expect(page.get_by_role("button", name="Save changes")).to_be_visible()
        expect(page.get_by_role("button", name="Save changes")).to_be_enabled()
        browser.close()
    return 3


def check(width: int) -> int:
    html = fixture_page()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html)
        assert not errors, errors
        groups = page.locator("#connected-repos .gh-owner-group:visible")
        assert groups.count() == 2
        assert groups.nth(0).get_attribute("data-owner") == "northwind-labs"
        assert groups.nth(1).get_attribute("data-owner") == "sam-rivera"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        row_height = page.locator(".gh-agent-row:visible").first.bounding_box()["height"]
        assert row_height == 56 if width >= 768 else row_height >= 56
        avatar = page.locator(
            "#connected-repos .gh-owner-heading .gh-owner-avatar, "
            "#connected-repos .gh-owner-heading .gh-owner-avatar-fallback"
        ).first
        row_icon = page.locator("#connected-repos .gh-row-icon").first
        assert abs(avatar.bounding_box()["x"] - row_icon.bounding_box()["x"]) <= 1
        assert (
            page.locator("#connected-repos .gh-owner-heading").first.bounding_box()["height"] == 40
        )
        check_rows(page)
        assert "northwind-labs/saved" in page.locator("#working-card").inner_text()
        page.locator("#change-working").click()
        page.locator("#working-choice").select_option("7701")
        page.locator("#confirm-working").click()
        assert "sam-rivera/saved" in page.locator("#working-card").inner_text()
        assert (
            page.locator("#connected-repos .gh-owner-group:visible").first.get_attribute(
                "data-owner"
            )
            == "sam-rivera"
        )

        page.get_by_role("button", name="Add repos", exact=True).click()
        assert page.locator("#repo-owner-choice").is_visible()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').check()
        page.get_by_role("button", name="Back").click()
        selected = page.locator('.gh-owner-button[data-owner="northwind-labs"] .gh-owner-selected')
        assert selected.inner_text() == "1 selected"
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7902"] input').check()
        assert page.locator("#picker-count").inner_text() == "2 selected"
        page.get_by_role("button", name="Back").click()
        assert page.locator("#owner-picker-count").inner_text() == "2 selected"
        page.locator("#owner-stage-repos").click()
        assert page.locator("#repo-picker").is_hidden()
        assert page.locator("#connected-repos .gh-agent-row.gh-added-row").count() == 2
        assert page.locator("#connected-repos .gh-owner-group:visible").count() == 3
        posted = page.locator("#github-connect-form").evaluate(
            "form => [...new FormData(form).getAll('repo')].sort()"
        )
        assert posted == ["7802", "7902"]
        assert page.locator("#github-connect-form").evaluate(
            "form => [...new FormData(form).keys()].every(key => !key.startsWith('access_'))"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator('input[name="installation"]').count() == 0
        assert page.get_by_text("Remove all and switch").count() == 0

        page.set_content(fixture_page(one_owner=True))
        page.get_by_role("button", name="Add repos", exact=True).click()
        assert page.locator("#repo-owner-sheet").is_visible()
        assert page.locator("#repo-owner-choice").is_hidden()
        page.locator("#close-repo-picker").click()

        page.set_content(fixture_page(error_owner=True, stale=True))
        assert page.locator('.gh-owner-group[data-owner="lost-org"]').is_visible()
        stale_row = page.locator('.gh-owner-group[data-owner="lost-org"] .gh-agent-row')
        assert stale_row.get_by_text("Hidden repo").count() == 1
        assert stale_row.locator(".gh-working-action").count() == 0
        stale_row.get_by_role("button", name="Remove Hidden repo").click()
        assert "9901" in page.locator("#github-connect-form").evaluate(
            "form => new FormData(form).getAll('remove')"
        )
        page.get_by_role("button", name="Add repos", exact=True).click()
        assert page.locator('.gh-owner-button[data-owner="tidewater-eap"]').is_disabled()
        assert page.locator('.gh-owner-button[data-owner="northwind-labs"]').is_enabled()

        calls = 0

        def respond(route: Route) -> None:
            nonlocal calls
            calls += 1
            body = fixture_page(error_owner=calls == 1)
            route.fulfill(status=200, content_type="text/html", body=body)

        page.route("https://mcp.test/oauth/github/confirm?state=fixture", respond)
        page.goto("https://mcp.test/oauth/github/confirm?state=fixture")
        page.get_by_role("button", name="Add repos", exact=True).click()
        assert page.locator("#repo-owner-choice").is_visible(), page.locator(
            "#repo-picker"
        ).inner_text()
        page.locator('.gh-owner-button[data-owner="northwind-labs"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7802"] input').check()
        page.get_by_role("button", name="Back").click()
        page.locator('.gh-owner-retry[data-owner="tidewater-eap"]').click()
        expect(page.locator('.gh-owner-button[data-owner="tidewater-eap"]')).to_be_enabled()
        assert page.locator("#owner-picker-count").inner_text() == "1 selected"
        page.locator('.gh-owner-button[data-owner="tidewater-eap"]').click()
        page.locator('.gh-picker-choice[data-repo-id="7902"] input').check()
        assert page.locator("#picker-count").inner_text() == "2 selected"
        browser.close()
    return 30


if __name__ == "__main__":
    count = (
        check(390)
        + check(1280)
        + check_staged_edits(390)
        + check_staged_edits(1280)
        + check_no_js()
    )
    capture(390)
    capture(1280)
    print(f"{count} browser checks passed; 12 screenshots saved to {SHOTS}")
