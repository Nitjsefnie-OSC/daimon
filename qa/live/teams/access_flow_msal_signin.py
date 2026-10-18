# pyright: basic
"""Saved access flow. Never inspect the callback URL or screenshot the page."""

from __future__ import annotations

import asyncio
import sys
from typing import Protocol


class Locator(Protocol):
    async def is_visible(self) -> bool: ...
    async def click(self) -> None: ...
    async def fill(self, value: str) -> None: ...
    async def get_attribute(self, name: str) -> str | None: ...
    async def inner_text(self) -> str: ...


async def run(page, ctx):  # type: ignore[no-untyped-def]
    step = "open"
    failed: str | None = None
    completed = False
    try:
        await page.goto(ctx.args["url"])
        seen: dict[str, int] = {}

        async def label_for(locator, fallback: str) -> str:  # type: ignore[no-untyped-def]
            aria = await locator.get_attribute("aria-label")
            if aria:
                return aria
            element_id = await locator.get_attribute("id")
            if element_id:
                associated = page.locator(f'label[for="{element_id}"]')
                if await associated.is_visible():
                    return await associated.inner_text()
            return fallback

        async def first_visible() -> tuple[str, Locator]:
            deadline = asyncio.get_running_loop().time() + 30
            options = (
                ("redirect", page.get_by_text("sign-in complete, you can close this tab")),
                ("account error", page.get_by_text("Your account or password is incorrect")),
                ("account error", page.get_by_text("You don't have access")),
                ("account error", page.get_by_text("Sign-in is blocked")),
                ("consent", page.get_by_role("button", name="Accept")),
                ("method choice", page.get_by_text("Use a verification code")),
                ("email", page.locator("input[name=loginfmt]")),
                ("password", page.locator("input[name=passwd]")),
                ("verification", page.locator("input[name=otc]")),
                ("stay signed in", page.get_by_text("Stay signed in?")),
            )
            while asyncio.get_running_loop().time() < deadline:
                for name, locator in options:
                    if await locator.is_visible():
                        return name, locator
                await asyncio.sleep(0.25)
            raise RuntimeError("state timeout")

        for _ in range(10):
            step, locator = await first_visible()
            seen[step] = seen.get(step, 0) + 1
            if seen[step] > 2:
                raise RuntimeError("repeated state")
            if step == "redirect":
                await ctx.expect(locator)
                completed = True
                break
            if step in {"consent", "account error"}:
                raise RuntimeError("sign-in refused")
            if step == "method choice":
                await locator.click()
            elif step == "email":
                await locator.fill(ctx.args["upn"])
                await page.get_by_role("button", name="Next").click()
            elif step == "password":
                label = await label_for(locator, "Enter your password")
                await ctx.fill(ctx.args["item"], "password", label, submit=True)
            elif step == "verification":
                label = await label_for(locator, "Enter code")
                try:
                    await ctx.fill(ctx.args["item"], "totp", label, submit=True)
                except Exception:
                    prompt = f"Enter the Microsoft authenticator code for {ctx.args['persona']}"
                    try:
                        await ctx.handoff("code", prompt, label)
                    except Exception:
                        await ctx.handoff("code", prompt, label, retry_of=ctx.last_card_id)
            elif step == "stay signed in":
                no_button = page.locator("#idBtn_Back")
                if await no_button.is_visible():
                    await no_button.click()
                else:
                    await page.get_by_role("button", name="No").click()
        if not completed:
            step = "state loop"
            raise RuntimeError("state limit")
    except Exception:
        failed = step
    finally:
        try:
            await page.goto("about:blank")
        except Exception:
            failed = "cleanup"
    if failed is not None:
        print(f"msal_signin failed at: {failed}", file=sys.stderr)
        raise RuntimeError(f"msal_signin failed at: {failed}") from None
