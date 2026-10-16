"""Group chat rosters: answered only while everyone there is ours."""

from __future__ import annotations

import uuid

import httpx
import pytest
from daimon.adapters.teams.group_chats import ChatMember, GroupChatRoster, outsiders

OURS = str(uuid.UUID(int=1))
ADA = ChatMember("29:ada", str(uuid.UUID(int=2)), OURS, "user")
BOT = ChatMember("28:bot", None, None, None)
GUEST = ChatMember("29:guest", str(uuid.UUID(int=3)), OURS, "guest")


@pytest.mark.parametrize(
    ("member", "count"),
    [
        (ADA, 0),
        (BOT, 0),
        (ChatMember("29:x", str(uuid.UUID(int=4)), str(uuid.UUID(int=99)), "user"), 1),
        (GUEST, 1),
        (ChatMember("29:anon", None, None, "anonymous"), 1),
        (ChatMember("29:nobody", str(uuid.UUID(int=5)), OURS, None), 1),
        (ChatMember("29:noid", None, OURS, "user"), 1),
    ],
)
def test_only_our_users_and_bots_are_not_outsiders(member: ChatMember, count: int) -> None:
    assert outsiders([ADA, member], tenant_id=OURS, member_guests=frozenset()) == count


def test_a_guest_is_ours_when_listed_or_guests_are_unrestricted() -> None:
    listed = frozenset({str(uuid.UUID(int=3))})
    assert outsiders([GUEST], tenant_id=OURS, member_guests=listed) == 0
    assert outsiders([GUEST], tenant_id=OURS, member_guests=None) == 0


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_the_roster_is_cached_briefly_and_a_failure_closes_the_chat() -> None:
    calls: list[str] = []
    rosters: list[list[ChatMember] | Exception] = [
        [ADA],
        httpx.ConnectError("down"),
        [ADA, GUEST],
    ]

    async def members(chat_id: str) -> list[ChatMember]:
        calls.append(chat_id)
        roster = rosters.pop(0)
        if isinstance(roster, Exception):
            raise roster
        return roster

    async def guests() -> frozenset[str]:
        return frozenset()

    clock = _Clock()
    roster = GroupChatRoster(tenant_id=OURS, members=members, member_guests=guests, clock=clock)
    assert not await roster.has_outsiders("19:c@thread.v2")
    assert not await roster.has_outsiders("19:c@thread.v2"), "cached"
    assert calls == ["19:c@thread.v2"]
    clock.now = 61
    assert await roster.has_outsiders("19:c@thread.v2"), "an unreadable roster fails closed"
    clock.now = 122
    assert await roster.has_outsiders("19:c@thread.v2"), "a guest joined"
    assert len(calls) == 3
