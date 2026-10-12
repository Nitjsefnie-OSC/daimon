"""Executable record: a Space decides the same on Discord, Slack and Teams.

The rules compare channel ids and Space keys, never their shape, so one own
Space of two channels gives its agent one home on every platform: it runs,
reads and posts in both, and nowhere else. The one asymmetry is Teams'
private and shared channels, which daimon reads only from a conversation
inside them; a Space puts a turn in one member inside the others, so such a
channel in a Space is read from any member
(`packages/adapters/mcp/tests/tools/test_teams_read.py`).

No database: a scope check over `authorize`.
"""

from __future__ import annotations

import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, SpaceRule, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize
from daimon.core.permissions import space_reach, with_space

KEY = "space:00000000-0000-4000-8000-000000000002"
# Two members and an outside channel, in each platform's id shape.
CHANNELS = {
    "discord": ("1100000000000000001", "1100000000000000002", "1100000000000000003"),
    "slack": ("C0000000A1", "C0000000A2", "C0000000A3"),
    "teams": ("19:a1@thread.tacv2", "19:a2@thread.tacv2", "19:a3@thread.tacv2"),
}


def _decisions(first: str, second: str, outside: str) -> tuple[bool, ...]:
    space = SpaceRule(
        key=KEY,
        name="client",
        rule=ChannelRule(readers="own", writers="own"),
        channel_ids=frozenset({first, second}),
    )
    base = TenantAccessPolicy(agent_rules={"x": AgentRule(runs_in=(first, second))})
    policy = with_space(base, KEY, space)

    def allowed(action: Action, place: str, origin: str | None = None) -> bool:
        return bool(
            authorize(
                policy,
                subject=Subject(platform_user_id="u1"),
                action=action,
                agent=AgentRef.of("x"),
                place=Place(channel_id=place),
                origin=Place(channel_id=origin) if origin else None,
                origin_channel_ids=space_reach(policy, {origin} if origin else set()),
            )
        )

    return (
        allowed(Action.RUN_AGENT, first),
        allowed(Action.RUN_AGENT, second),
        allowed(Action.RUN_AGENT, outside),
        allowed(Action.READ_CHANNEL, second, origin=first),
        allowed(Action.POST, second, origin=first),
        allowed(Action.READ_CHANNEL, outside, origin=first),
        allowed(Action.POST, outside, origin=first),
    )


@pytest.mark.parametrize("platform", sorted(CHANNELS))
def test_one_space_home_decides_alike_on_every_platform(platform: str) -> None:
    assert _decisions(*CHANNELS[platform]) == (True, True, False, True, True, False, False), (
        f"{platform}: the Space's agent works across its members and nowhere else"
    )
