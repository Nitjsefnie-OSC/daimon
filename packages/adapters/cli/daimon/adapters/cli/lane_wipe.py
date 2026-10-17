"""Claim and wipe an Anthropic workspace owned by one dev lane."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import list_skills_strict
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_WORKSPACE,
    MA_METADATA_VALUE_WORKSPACE_DISPOSABLE,
)
from daimon.core.ma import delete_entire_workspace_for_testing

if TYPE_CHECKING:
    from collections.abc import Sequence

MARKER_PREFIX = "daimon-lane-marker-"


class WorkspaceNotOwned(Exception):
    """The supplied key cannot be shown to belong to this lane."""


def require_lane_environment() -> str:
    """Reject non-lane names before creating an API client."""
    name = os.environ.get("DAIMON_ENVIRONMENT__NAME", "")
    if re.fullmatch(r"lane-[a-z][a-z0-9-]{1,19}", name) is None:
        raise RuntimeError("workspace wipe requires DAIMON_ENVIRONMENT__NAME=lane-<lane>")
    return name


def _marker_name(lane_environment: str) -> str:
    return MARKER_PREFIX + lane_environment.removeprefix("lane-")


def _is_marker(agent: object, name: str) -> bool:
    return (
        getattr(agent, "name", None) == name
        and getattr(agent, "archived_at", None) is None
        and (getattr(agent, "metadata", {}) or {}).get(MA_METADATA_KEY_WORKSPACE)
        == MA_METADATA_VALUE_WORKSPACE_DISPOSABLE
    )


async def _create_marker(client: AsyncAnthropic, name: str) -> None:
    # An idle agent costs no session and doubles as the existing cleanup helper's
    # disposable sentinel. The helper leaves one such agent in place.
    await client.beta.agents.create(
        model="claude-haiku-5-5",
        name=name,
        description="Ownership marker for a disposable dev lane workspace.",
        metadata={MA_METADATA_KEY_WORKSPACE: MA_METADATA_VALUE_WORKSPACE_DISPOSABLE},
    )


async def check_workspace(client: AsyncAnthropic, lane_environment: str) -> None:
    """Claim an empty workspace or verify this lane's marker, else refuse."""
    if require_lane_environment() != lane_environment:
        raise RuntimeError("workspace lane does not match DAIMON_ENVIRONMENT__NAME")
    name = _marker_name(lane_environment)
    agents = [agent async for agent in client.beta.agents.list(limit=100, include_archived=True)]
    if any(_is_marker(agent, name) for agent in agents):
        return

    refused = WorkspaceNotOwned(f"workspace not owned by {lane_environment}; refusing")
    if agents:
        raise refused
    for resource in (
        client.beta.environments,
        client.beta.vaults,
        client.beta.memory_stores,
    ):
        async for _ in resource.list(limit=100, include_archived=True):
            raise refused
    # Built-in Anthropic skills do not belong to a customer workspace. Custom
    # skills are deleted by the cleanup helper, so they must block first claim.
    if any(skill.source == "custom" for skill in await list_skills_strict(client)):
        raise refused
    await _create_marker(client, name)


async def wipe_workspace(client: AsyncAnthropic, lane_environment: str) -> None:
    """Verify ownership, wipe, then ensure the marker survives cleanup."""
    await check_workspace(client, lane_environment)
    await delete_entire_workspace_for_testing(client, i_understand_this_destroys_all_tenants=True)
    name = _marker_name(lane_environment)
    remaining = [agent async for agent in client.beta.agents.list(limit=100)]
    if not any(_is_marker(agent, name) for agent in remaining):
        await _create_marker(client, name)


async def _run(command: str, lane_environment: str) -> None:
    key = os.environ["DAIMON_ANTHROPIC__API_KEY"]
    async with AsyncAnthropic(api_key=key) as client:
        if command == "check":
            await check_workspace(client, lane_environment)
        else:
            await wipe_workspace(client, lane_environment)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "wipe"))
    args = parser.parse_args(argv)
    lane_environment = require_lane_environment()
    try:
        asyncio.run(_run(args.command, lane_environment))
    except WorkspaceNotOwned as err:
        print(str(err), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
