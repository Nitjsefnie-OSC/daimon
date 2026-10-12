"""Spaces in the pure model: one home across several channels, and nothing outside it."""

from __future__ import annotations

import pytest
from daimon.core.access_policy import SpaceRule, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, SessionFacts, Subject, authorize
from daimon.core.permissions import (
    AgentRule,
    ChannelRule,
    agent_permissions,
    channel_permissions,
    channel_rule,
    describe_home,
    memory_writable,
    readable_from,
    runs_only_in,
    session_readable_from,
    with_space,
)

KEY = "space:00000000-0000-4000-8000-000000000001"
OWN = ChannelRule(readers="own", writers="own")
INSIDE = ChannelRule(readers="inside")
X = AgentRef.of("x")
MEMBER = Subject(platform_user_id="u1")


def _policy(
    rule: ChannelRule = OWN, *, runs_in: tuple[str, ...] = ("a", "b"), members: str = "ab"
) -> TenantAccessPolicy:
    """Space `client` of channels a and b; agent x runs in `runs_in`; c is outside."""
    space = SpaceRule(key=KEY, name="client", rule=rule, channel_ids=frozenset(members))
    base = TenantAccessPolicy(agent_rules={"x": AgentRule(runs_in=runs_in)})
    return with_space(base, KEY, space)


def _decide(
    policy: TenantAccessPolicy,
    action: Action,
    place: str,
    *,
    origin: str | None = None,
    agent: AgentRef = X,
    session: SessionFacts | None = None,
) -> str | None:
    """None when allowed, else the reason."""
    decision = authorize(
        policy,
        subject=MEMBER,
        action=action,
        agent=agent,
        place=Place(channel_id=place),
        origin=Place(channel_id=origin) if origin is not None else None,
        origin_channel_ids=frozenset({origin}) if origin is not None else frozenset(),
        session=session,
    )
    return None if decision else decision.reason


def test_space_rule_is_written_onto_each_member_and_never_serialized() -> None:
    policy = _policy()
    assert channel_rule(policy, "a") == OWN and channel_rule(policy, "b") == OWN, (
        "each member carries the Space rule, so a build without Spaces keeps it contained"
    )
    dumped = policy.model_dump(mode="json")
    assert "spaces" not in dumped, "the policy row must stay readable by an older build"
    assert TenantAccessPolicy.model_validate(dumped).spaces == {}, "a dump round-trips"


def test_agent_naming_members_has_the_space_as_home() -> None:
    policy = _policy()
    assert agent_permissions(policy, ("x",)).home == KEY, "two members of one own Space: one home"
    assert runs_only_in(policy, "a") == ("x",), "x is an own agent of every member"
    assert describe_home(policy, KEY) == "Space client", "a Space home reads by its name"
    assert agent_permissions(policy, ("x",)).budget_channel is None, "a run is charged where it is"
    wider = _policy(runs_in=("a", "b", "c"))
    assert agent_permissions(wider, ("x",)).home is None, "a channel outside: no home"


@pytest.mark.parametrize("place", ["a", "b"])
def test_space_agent_runs_reads_writes_memory_and_answers_externals_in_every_member(
    place: str,
) -> None:
    policy = _policy()
    here = channel_permissions(policy, channel_id=place)
    assert here.home == KEY and here.answers_external, "external people are answered in members"
    assert memory_writable(agent_permissions(policy, ("x",)), here), "memory is written at home"
    assert _decide(policy, Action.RUN_AGENT, place) is None, "x runs in every member"
    other = "b" if place == "a" else "a"
    assert _decide(policy, Action.READ_CHANNEL, other, origin=place) is None, "reads across"
    assert _decide(policy, Action.POST, other, origin=place) is None, "posts across"


def test_nothing_crosses_the_space_boundary() -> None:
    policy = _policy()
    assert _decide(policy, Action.RUN_AGENT, "c") == "runs_elsewhere", "x runs nowhere else"
    assert _decide(policy, Action.READ_CHANNEL, "c", origin="a") == "own_agents_only", (
        "held to the Space, x reads nothing outside it"
    )
    assert _decide(policy, Action.POST, "c", origin="a") == "own_agents_only", "nor posts there"
    shared = AgentRef.of("y")
    assert _decide(policy, Action.RUN_AGENT, "a", agent=shared) == "own_agents_only", (
        "an agent that is not the Space's own is refused in it"
    )
    assert not readable_from(policy, {"c"}, "a"), "a member is unreadable from outside"


def test_a_members_session_is_readable_from_another_member_only() -> None:
    policy = _policy()
    ran_in_a = SessionFacts(channel="a", seal_ids=frozenset({"a"}), owned=True)
    assert session_readable_from(
        policy, {"b"}, channel="a", thread=None, seal_ids={"a"}, legacy_thread_id=None
    ), "a turn in b is inside a"
    assert _decide(policy, Action.READ_SESSION, "b", origin="b", session=ran_in_a) is None
    assert _decide(policy, Action.READ_SESSION, "c", origin="c", session=ran_in_a) is not None, (
        "a turn outside the Space does not read it"
    )
    left = with_space(
        policy, KEY, SpaceRule(key=KEY, name="client", rule=OWN, channel_ids=frozenset("b"))
    )
    assert channel_rule(left, "a") == OWN, "a channel leaving keeps the rule, so stays contained"
    assert not session_readable_from(
        left, {"b"}, channel="a", thread=None, seal_ids={"a"}, legacy_thread_id=None
    ), "once a leaves, its sessions are a's alone again"


def test_inside_space_members_read_each_other_and_keep_no_agents() -> None:
    policy = _policy(INSIDE)
    assert readable_from(policy, {"a"}, "b"), "a turn in a is inside b"
    assert not readable_from(policy, {"c"}, "b"), "but not one in c"
    assert agent_permissions(policy, ("x",)).home is None, "an inside Space is no home"
    assert _decide(policy, Action.RUN_AGENT, "a", agent=AgentRef.of("y")) is None, "any agent runs"


def test_set_space_is_for_server_admins_only() -> None:
    policy = _policy()
    admin = Subject(is_admin=True, platform_user_id="u1")
    key = Subject(is_admin=True, platform_user_id="u1", via_agent_key=True)
    assert authorize(policy, subject=admin, action=Action.SET_SPACE), "an admin sets Spaces"
    assert not authorize(policy, subject=MEMBER, action=Action.SET_SPACE), "a member doesn't"
    assert not authorize(policy, subject=key, action=Action.SET_SPACE), "nor an agent key"
