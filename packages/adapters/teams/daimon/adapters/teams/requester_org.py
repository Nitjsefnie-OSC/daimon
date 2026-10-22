"""Which organisation a Teams sender belongs to, settled by a member lookup.

A message's own fields often cannot say (`identity.classify_requester`): a shared
channel, a group chat, a replayed message and a queued continuation carry no
positive evidence that the sender is one of ours. The Bot Framework single-member
read does: it names the person's tenant with the bot's own token and no Graph
permission. This module reads it, reduces it to a `RequesterOrg`, and remembers the
answer so a busy channel does not read the roster per message.

Fail-closed: a lookup that errors, times out, finds no one or names no tenant leaves
the sender UNKNOWN, which core holds to the one channel for that turn only.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog
from daimon.adapters.teams.identity import canonical_uuid, channel_shared_of
from daimon.adapters.teams.lifecycle import SEND_TIMEOUT_S
from daimon.core.teams_bot_framework import SERVICE_URL
from daimon.core.turn_hold import RequesterOrg
from microsoft_teams.apps import App

__all__ = [
    "FOUND_TTL_S",
    "MISSED_TTL_S",
    "MemberFacts",
    "ChannelTypeRead",
    "MemberRead",
    "RequesterOrgResolver",
    "SdkMemberRead",
    "Verdict",
    "verdict_of",
]

log = structlog.get_logger()

FOUND_TTL_S = 300.0
"""How long a settled answer (host or other) is reused."""
MISSED_TTL_S = 15.0
"""How long a failed lookup is reused: long enough to absorb a burst, short enough to retry."""
_CACHE_CAP = 2048
# The Bot Framework role of someone who joined without signing in.
_ANONYMOUS = "anonymous"

type Verdict = tuple[RequesterOrg, str | None]
_UNKNOWN: Verdict = (RequesterOrg.UNKNOWN, None)


@dataclass(frozen=True)
class MemberFacts:
    """What the single-member read says about one person."""

    aad_object_id: str | None
    tenant_id: str | None
    role: str | None


type ChannelTypeRead = Callable[[str, str], Awaitable[str | None]]
"""A channel's Bot Framework type (`standard`, `private`, `shared`) given its team and id."""


class MemberRead(Protocol):
    """The one Bot Framework call a lookup needs."""

    async def __call__(self, conversation_id: str, aad_object_id: str) -> MemberFacts | None:
        """The person's facts in the conversation; None when it has no such member."""
        ...


class SdkMemberRead:
    """`MemberRead` over the SDK app, timed: the SDK's HTTP client sets no timeout."""

    def __init__(self, app: App) -> None:
        self._app = app

    async def __call__(self, conversation_id: str, aad_object_id: str) -> MemberFacts | None:
        conversations = self._app.api.from_service_url(SERVICE_URL).conversations
        try:
            account = await asyncio.wait_for(
                conversations.get_member_by_id(conversation_id, aad_object_id), SEND_TIMEOUT_S
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (403, 404):
                return None
            raise
        return MemberFacts(
            aad_object_id=account.aad_object_id,
            tenant_id=account.tenant_id,
            role=account.user_role,
        )


def verdict_of(facts: MemberFacts | None, *, ours: str, aad_object_id: str) -> Verdict:
    """The organisation one member read proves, or UNKNOWN.

    HOST needs the same person back, our tenant, and a role other than anonymous (a
    guest of ours counts: they belong to the tenant). A different tenant is OTHER.
    """
    if facts is None or canonical_uuid(facts.aad_object_id) != canonical_uuid(aad_object_id):
        return _UNKNOWN
    tenant = (facts.tenant_id or "").strip()
    if not tenant:
        return _UNKNOWN
    if canonical_uuid(tenant) != ours:
        return RequesterOrg.OTHER, canonical_uuid(tenant) or tenant
    if (facts.role or "").strip().lower() == _ANONYMOUS:
        return _UNKNOWN
    return RequesterOrg.HOST, None


class RequesterOrgResolver:
    """Looks senders up and caches the answer per (conversation, user).

    A settled answer is kept for `FOUND_TTL_S`, a failure only for `MISSED_TTL_S`. A
    dedicated cache, not the roster cache: it stores one verdict per person, not a
    set of members.
    """

    def __init__(
        self,
        member_read: MemberRead | None,
        *,
        ours: str,
        channel_type_read: ChannelTypeRead | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = member_read
        self._channel_type_read = channel_type_read
        self._shared: OrderedDict[tuple[str, str], tuple[float, bool | None]] = OrderedDict()
        self._ours = ours
        self._clock = clock
        self._cache: OrderedDict[tuple[str, str], tuple[float, Verdict]] = OrderedDict()

    async def resolve(self, conversation_id: str, aad_object_id: str) -> Verdict:
        """The sender's organisation by member lookup; UNKNOWN on any failure."""
        key = (conversation_id, aad_object_id.lower())
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        verdict = await self._lookup(conversation_id, aad_object_id)
        settled = verdict[0] is not RequesterOrg.UNKNOWN
        self._remember(key, verdict, now + (FOUND_TTL_S if settled else MISSED_TTL_S))
        return verdict

    async def resolve_shared(self, team_id: str, channel_id: str) -> bool | None:
        """Whether the channel is shared, from its explicit type; None when that is not known.

        Independent of who is asking: a roster that confirms a member says nothing about
        whether outsiders are in the channel too. Only an explicit type counts.
        """
        key = (team_id, channel_id)
        now = self._clock()
        cached = self._shared.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        shared = await self._channel_shared(team_id, channel_id)
        ttl = FOUND_TTL_S if shared is not None else MISSED_TTL_S
        self._shared[key] = (now + ttl, shared)
        self._shared.move_to_end(key)
        while len(self._shared) > _CACHE_CAP:
            self._shared.popitem(last=False)
        return shared

    async def _channel_shared(self, team_id: str, channel_id: str) -> bool | None:
        if self._channel_type_read is None:
            return None
        try:
            async with asyncio.timeout(SEND_TIMEOUT_S):
                kind = await self._channel_type_read(team_id, channel_id)
        except (httpx.HTTPError, TimeoutError, ValueError) as err:
            log.warning("teams.channel_type_lookup.failed", error=type(err).__name__)
            return None
        return channel_shared_of(kind)

    async def _lookup(self, conversation_id: str, aad_object_id: str) -> Verdict:
        if self._read is None:
            return _UNKNOWN
        try:
            facts = await self._read(conversation_id, aad_object_id)
        except (httpx.HTTPError, TimeoutError, ValueError) as err:
            log.warning("teams.requester_lookup.failed", error=type(err).__name__)
            return _UNKNOWN
        return verdict_of(facts, ours=self._ours, aad_object_id=aad_object_id)

    def _remember(self, key: tuple[str, str], verdict: Verdict, expires: float) -> None:
        self._cache[key] = (expires, verdict)
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_CAP:
            self._cache.popitem(last=False)
