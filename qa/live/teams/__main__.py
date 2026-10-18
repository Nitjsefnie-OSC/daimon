"""Teams persona QA operator commands."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.graph import CHAT, READ, SEND, TARGET, GraphTeamsDriver
from qa.live.teams.journeys import resolve_journey, run_journey
from qa.live.teams.login import (
    AccessFlowAuthorizer,
    install_flow,
    login_with_access_flow,
    manual_login,
)
from qa.live.teams.tokens import ReauthRequired, TokenStore


async def execute(args: argparse.Namespace, config: TeamsQaConfig) -> None:
    """Execute one operator command after CLI validation."""
    tokens = TokenStore(config)
    if args.command == "install-flow":
        print(install_flow(config.company))
    elif args.command == "status":
        print(json.dumps(tokens.status(args.persona)))
    elif args.command == "logout":
        tokens.logout(args.persona)
        print("cache deleted; also revoke the session in My Sign-ins or delete the app grant")
    elif args.command == "login":
        scopes = sorted(set(CHAT + READ + SEND + TARGET))
        if args.access_flow:
            identity = config.personas[args.persona]
            authorizer = AccessFlowAuthorizer(config.company, identity.vault_item, identity.upn)
            await login_with_access_flow(tokens, args.persona, scopes, authorizer)
        else:
            manual_login(tokens, args.persona, scopes)
        print("sign-in complete")
    else:
        async with httpx.AsyncClient(timeout=15) as http:
            driver = GraphTeamsDriver(config, tokens, http)
            if args.command == "verify-target":
                await driver.verify_target(args.persona)
                print("target verified")
            elif args.command == "smoke":
                journey = resolve_journey(config, args.journey)
                await driver.verify_target(journey.persona)
                result = await run_journey(driver, journey)
                path = result.write(args.results)
                print(f"{path} {result.classification}")


def main(argv: list[str] | None = None) -> int:
    """Parse the Teams QA CLI and print only safe errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["install-flow", "login", "status", "logout", "verify-target", "smoke"]
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--persona")
    login_mode = parser.add_mutually_exclusive_group()
    login_mode.add_argument(
        "--manual", action="store_true", help="paste redirect privately (default)"
    )
    login_mode.add_argument("--access-flow", action="store_true", help="use saved browser flow")
    parser.add_argument("--journey")
    parser.add_argument("--max-turns", type=int, default=1)
    parser.add_argument("--go", action="store_true")
    parser.add_argument("--results", type=Path)
    args = parser.parse_args(argv)
    if args.command != "login" and (args.manual or args.access_flow):
        parser.error("login mode flags require login")
    if args.command == "smoke":
        if not args.go:
            parser.error("smoke requires --go")
        if not 1 <= args.max_turns <= 4:
            parser.error("max-turns must be 1..4")
        if not args.journey or args.results is None:
            parser.error("smoke requires --journey and --results")
    elif args.command in {"login", "status", "logout", "verify-target"} and not args.persona:
        parser.error("--persona is required")
    try:
        config = TeamsQaConfig.load(args.config)
        asyncio.run(execute(args, config))
    except ReauthRequired as exc:
        parser.exit(1, f"{exc}\n")
    except Exception as exc:
        parser.exit(1, f"Teams QA command failed: {type(exc).__name__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
