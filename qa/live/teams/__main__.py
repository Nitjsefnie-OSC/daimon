"""Teams persona QA operator commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.evidence import write_summary
from qa.live.teams.graph import CHAT, TARGET, GraphTeamsDriver
from qa.live.teams.harness import read, send, wait
from qa.live.teams.journeys import resolve_journey, run_journey
from qa.live.teams.login import (
    AccessFlowAuthorizer,
    install_flow,
    login_with_access_flow,
    manual_login,
)
from qa.live.teams.tokens import ReauthRequired, TokenStore, token_claims

ALL_JOURNEYS = ("routines-admin", "routines-member", "billing-admin", "billing-member")
HARNESS_VERBS = ("send", "read", "wait")


async def execute(args: argparse.Namespace, config: TeamsQaConfig) -> bool:
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
        scopes = sorted(set(CHAT + TARGET))
        if args.access_flow:
            identity = config.personas[args.persona]
            authorizer = AccessFlowAuthorizer(config.company, identity.vault_item, identity.upn)
            await login_with_access_flow(tokens, args.persona, scopes, authorizer)
        else:
            manual_login(tokens, args.persona, scopes)
        print("sign-in complete")
    elif args.command == "refresh":
        successful = True
        for persona in config.personas if args.all else (args.persona,):
            try:
                secret = tokens.acquire(persona, sorted(set(CHAT + TARGET)), force_refresh=True)
                expiry = token_claims(secret.value).get("exp")
                access_expiry = (
                    datetime.fromtimestamp(expiry, UTC).isoformat()
                    if isinstance(expiry, int)
                    else tokens.status(persona)["expiry"]
                )
                print(
                    json.dumps(
                        {
                            "persona": persona,
                            "ok": True,
                            "version_added": tokens.version_added,
                            "access_expiry": access_expiry,
                        }
                    )
                )
            except ReauthRequired as exc:
                successful = False
                print(
                    json.dumps(
                        {
                            "persona": persona,
                            "ok": False,
                            "version_added": False,
                            "access_expiry": None,
                        }
                    )
                )
                hint = f"{exc}; run: python -m qa.live.teams login --persona {persona}"
                secret_name = args.config_secret or os.environ.get("DAIMON_TEAMS_QA_CONFIG_SECRET")
                in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
                if in_actions:
                    # Public Actions logs: name the variable, not the project.
                    secret_name = secret_name and "<TEAMS_QA_CONFIG_SECRET>"
                if args.config_secret or (
                    secret_name and not (args.config or os.environ.get("DAIMON_TEAMS_QA_CONFIG"))
                ):
                    hint += f" --config-secret {secret_name}"
                print(hint, file=sys.stderr)
                if in_actions:
                    print(f"::error title=Teams QA reauth::{hint}", file=sys.stderr)
        return successful
    else:
        async with httpx.AsyncClient(timeout=15) as http:
            driver = GraphTeamsDriver(config, tokens, http)
            if args.command in HARNESS_VERBS:
                if args.command == "send":
                    result = await send(driver, args.persona, args.to, args.text)
                elif args.command == "read":
                    result = await read(driver, args.persona, args.source, args.since)
                else:
                    result = await wait(driver, args.persona, args.after, args.timeout, args.source)
                print(json.dumps(result))
                return bool(result["ok"])
            if args.command == "verify-target":
                await driver.verify_target(args.persona)
                print("target verified")
            elif args.command == "smoke":
                names = ALL_JOURNEYS if args.all else (args.journey,)
                journeys = {name: resolve_journey(config, name) for name in names}
                for persona in dict.fromkeys(journey.persona for journey in journeys.values()):
                    await driver.verify_target(persona)
                results = {
                    name: await run_journey(driver, journey) for name, journey in journeys.items()
                }
                if args.all:
                    path = write_summary(args.results, results)
                    summary = " ".join(f"{name}={r.classification}" for name, r in results.items())
                    print(f"{path} {summary}")
                else:
                    result = results[args.journey]
                    path = result.write(args.results)
                    print(f"{path} {result.classification}")
    return True


def main(argv: list[str] | None = None) -> int:
    """Parse the Teams QA CLI and print only safe errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "install-flow",
            "login",
            "status",
            "logout",
            "verify-target",
            "smoke",
            "refresh",
            *HARNESS_VERBS,
        ],
    )
    parser.add_argument("text", nargs="?", help="message text for send")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--config-secret")
    parser.add_argument("--persona")
    login_mode = parser.add_mutually_exclusive_group()
    login_mode.add_argument(
        "--manual", action="store_true", help="paste redirect privately (default)"
    )
    login_mode.add_argument("--access-flow", action="store_true", help="use saved browser flow")
    smoke_choice = parser.add_mutually_exclusive_group()
    smoke_choice.add_argument("--journey")
    smoke_choice.add_argument("--all", action="store_true", help="run all four built-in journeys")
    parser.add_argument("--max-turns", type=int, default=1)
    parser.add_argument("--go", action="store_true")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--to", choices=["chat", "channel"], help="send target")
    parser.add_argument(
        "--from", dest="source", default="chat", help="chat, channel or thread:<message id>"
    )
    parser.add_argument("--since", default="5m", help="read window, for example 30s, 5m, 1h")
    parser.add_argument("--after", help="wait for bot messages after this message id")
    parser.add_argument("--timeout", type=float, default=60.0, help="wait timeout in seconds")
    args = parser.parse_intermixed_args(argv)
    if args.command != "send" and (args.text is not None or args.to is not None):
        parser.error("text and --to require send")
    if args.command != "login" and (args.manual or args.access_flow):
        parser.error("login mode flags require login")
    if args.command not in {"smoke", "refresh"} and (args.journey or args.all):
        parser.error("--all requires smoke or refresh")
    if args.command == "smoke":
        if not args.go:
            parser.error("smoke requires --go")
        if not 1 <= args.max_turns <= 4:
            parser.error("max-turns must be 1..4")
        if not (args.journey or args.all) or args.results is None:
            parser.error("smoke requires --journey or --all, and --results")
    elif args.command == "refresh":
        if args.journey or args.all == bool(args.persona):
            parser.error("refresh requires exactly one of --all or --persona")
        if args.persona and not re.fullmatch(r"[A-Za-z0-9_-]+", args.persona):
            parser.error("invalid persona")
    elif args.command in {"login", "status", "logout", "verify-target", *HARNESS_VERBS}:
        if not args.persona:
            parser.error("--persona is required")
        if args.command == "send" and (args.to is None or not args.text):
            parser.error("send requires --to and text")
        if args.command == "wait" and not args.after:
            parser.error("wait requires --after")
    try:
        config = (
            TeamsQaConfig.load(args.config, args.config_secret)
            if args.config_secret
            else TeamsQaConfig.load(args.config)
        )
        if args.persona and args.persona not in config.personas:
            parser.error("unknown persona")
        success = asyncio.run(execute(args, config))
        return 0 if success else 1
    except ReauthRequired as exc:
        parser.exit(1, f"{exc}\n")
    except Exception as exc:
        if args.command in HARNESS_VERBS:
            failure = {"ok": False, "error": "command_failed", "type": type(exc).__name__}
            print(json.dumps(failure))
            return 1
        parser.exit(1, f"Teams QA command failed: {type(exc).__name__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
