"""One-shot loopback MSAL sign-in, invoked only by the login command."""

from __future__ import annotations

import asyncio
import getpass
import re
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from qa.live.teams.tokens import ReauthRequired, TokenStore


class BrowserAuthorizer(Protocol):
    async def authorize(self, auth_url: str, persona: str) -> None: ...


class FlowFailed(RuntimeError):
    """A fixed, token-safe browser flow failure."""


SAFE_FLOW_STEPS = frozenset(
    {
        "open",
        "email",
        "password",
        "verification",
        "method choice",
        "stay signed in",
        "consent",
        "account error",
        "redirect",
        "state loop",
        "cleanup",
    }
)


class AccessFlowAuthorizer:
    def __init__(self, company: str, vault_item: str, upn: str) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", company):
            raise ValueError("invalid company name")
        self.company, self.vault_item, self.upn = company, vault_item, upn

    async def authorize(self, auth_url: str, persona: str) -> None:
        """Run the optional saved flow without echoing subprocess output."""
        process = await asyncio.create_subprocess_exec(
            "access",
            "flow",
            "run",
            f"{self.company}/msal_signin",
            "--arg",
            f"url={auth_url}",
            "--arg",
            f"item={self.vault_item}",
            "--arg",
            f"upn={self.upn}",
            "--arg",
            f"persona={persona}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, _stderr = await process.communicate()
        if process.returncode:
            lines = _stderr.decode("utf-8", "replace").splitlines()
            reasons = [
                line
                for line in lines
                if line.startswith("msal_signin failed at: ")
                and line.removeprefix("msal_signin failed at: ") in SAFE_FLOW_STEPS
            ]
            reason = reasons[0] if reasons else "operation failed"
            raise FlowFailed(f"browser authorization failed (exit {process.returncode}): {reason}")


async def receive_redirect(
    expected_state: str,
    timeout: float,
    authorize: BrowserAuthorizer,
    auth_url: str,
    persona: str,
    port: int = 0,
    on_listen: Callable[[int], tuple[str, str]] | None = None,
) -> dict[str, str]:
    """Wait for one matching localhost auth response, ignoring preconnects."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[dict[str, str]] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
            except TimeoutError:
                line = b""
            parts = line.decode("ascii", "replace").split(" ")
            target = urlsplit(parts[1]) if len(parts) >= 2 else None
            query = parse_qs(target.query, keep_blank_values=True) if target else {}
            redirect = (
                len(parts) >= 2
                and parts[0] == "GET"
                and target is not None
                and target.path in {"", "/"}
                and bool({"code", "error", "state"} & query.keys())
            )
            valid = redirect and query.get("state") == [expected_state] and "error" not in query
            if redirect and not result.done():
                if "error" in query:
                    error = query["error"][0]
                    reason = (
                        f"authorization error {error}"
                        if re.fullmatch(r"[a-z_]+", error)
                        else "authorization error"
                    )
                    result.set_exception(ReauthRequired(persona, reason))
                elif valid:
                    result.set_result({key: values[0] for key, values in query.items()})
                else:
                    result.set_exception(ReauthRequired(persona, "state mismatch"))
            page = b"sign-in complete, you can close this tab" if valid else b"sign-in rejected"
            status = (
                b"200 OK" if valid else b"404 Not Found" if not redirect else b"400 Bad Request"
            )
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                + page
            )
            with suppress(OSError):
                await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    task: asyncio.Task[None] | None = None
    try:
        if on_listen is not None:
            expected_state, auth_url = on_listen(server.sockets[0].getsockname()[1])
        deadline = loop.time() + timeout
        task = asyncio.create_task(authorize.authorize(auth_url, persona))
        done, _ = await asyncio.wait(
            {task, result}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if result in done:
            task.cancel()
            return result.result()
        if task in done:
            task.result()
            return await asyncio.wait_for(result, max(0.01, deadline - loop.time()))
        raise ReauthRequired(persona, "sign-in timed out")
    except TimeoutError:
        raise ReauthRequired(persona, "sign-in timed out") from None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()


def manual_login(tokens: TokenStore, persona: str, scopes: list[str]) -> None:
    """Complete the auth-code flow from a privately pasted localhost redirect."""
    if not sys.stdin.isatty():
        raise ReauthRequired(persona, "manual login requires a TTY")
    client, cache = tokens.client_for_login(persona)
    upn = tokens.config.personas[persona].upn
    try:
        flow = client.initiate_auth_code_flow(
            scopes, redirect_uri="http://localhost", login_hint=upn, prompt="login"
        )
        auth_uri = flow["auth_uri"]
        state = flow["state"]
        if not isinstance(auth_uri, str) or not isinstance(state, str):
            raise ReauthRequired(persona, "authorization failed")
        print(auth_uri)
        print(
            f"Open this URL in a private window and sign in as {upn}. "
            "The page will fail to load; that is expected. "
            "Copy the full address from the address bar."
        )
        pasted = getpass.getpass("Paste full address: ")
        response = parse_manual_redirect(pasted, state, persona)
        result = client.acquire_token_by_auth_code_flow(flow, response)
        tokens.validate(persona, result)
        tokens.save(persona, cache)
    except ReauthRequired:
        raise
    except Exception:
        raise ReauthRequired(persona, "authorization failed") from None


def parse_manual_redirect(address: str, state: str, persona: str) -> dict[str, str]:
    """Validate the pasted redirect while keeping its code out of errors."""
    parsed = urlsplit(address.strip())
    if parsed.scheme != "http" or parsed.netloc != "localhost" or parsed.path not in {"", "/"}:
        raise ReauthRequired(persona, "redirect address mismatch")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "error" in query:
        error = query["error"][0]
        reason = (
            f"authorization error {error}"
            if re.fullmatch(r"[a-z_]+", error)
            else "authorization error"
        )
        raise ReauthRequired(persona, reason)
    if query.get("state") != [state]:
        raise ReauthRequired(persona, "state mismatch")
    if len(query.get("code", [])) != 1 or not query["code"][0]:
        raise ReauthRequired(persona, "authorization code missing")
    return {key: values[0] for key, values in query.items()}


async def login_with_access_flow(
    tokens: TokenStore,
    persona: str,
    scopes: list[str],
    authorizer: BrowserAuthorizer,
    timeout: float = 600,
) -> None:
    """Complete sign-in through the optional saved browser flow."""
    client, cache = tokens.client_for_login(persona)
    flow: dict[str, object] = {}

    def start(port: int) -> tuple[str, str]:
        redirect = f"http://localhost:{port}"
        flow.update(
            client.initiate_auth_code_flow(
                scopes,
                redirect_uri=redirect,
                login_hint=tokens.config.personas[persona].upn,
                prompt="login",
            )
        )
        return str(flow["state"]), str(flow["auth_uri"])

    try:
        response = await receive_redirect("", timeout, authorizer, "", persona, on_listen=start)
        result = client.acquire_token_by_auth_code_flow(flow, response)
        tokens.validate(persona, result)
        tokens.save(persona, cache)
    except ReauthRequired:
        raise
    except FlowFailed as exc:
        raise ReauthRequired(persona, str(exc)) from None
    except Exception:
        raise ReauthRequired(persona, "authorization failed") from None


def install_flow(company: str) -> Path:
    """Install the optional saved browser flow with private file permissions."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", company):
        raise ValueError("invalid company name")
    target = Path.home() / ".local/share/access/flows" / company / "msal_signin.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(Path(__file__).with_name("access_flow_msal_signin.py").read_bytes())
    target.chmod(0o600)
    return target
