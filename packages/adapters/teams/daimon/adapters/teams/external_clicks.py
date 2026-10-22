"""Clicks from people outside the organisation that added Daimon.

A channel shared with another organisation lets its members press the bot's cards.
Like an external sender's message, their click is limited: only the requester-bound
controls (Stop, Approve and Deny, feedback on their own answer) answer them, and
every other control refuses with `EXTERNAL_REFUSAL`.

`host_only` wraps a handler that must stay internal. It decides from the click's
own fields and, for a clicker they cannot place, the member lookup; whoever cannot
be placed counts as outside, as an unplaced sender is held. The requester-bound
handlers are registered unwrapped: they check the turn's author themselves, and
`own_thread_only` wraps the feedback handlers, which do not.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from daimon.adapters.teams.card_actions import submitted_fields
from daimon.adapters.teams.identity import canonical_uuid, classify_sender, live_tenant_id
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.platform_words import words_for
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn_hold import RequesterOrg
from microsoft_teams.api import InvokeActivity
from microsoft_teams.apps import ActivityContext

__all__ = [
    "EXTERNAL_REFUSAL",
    "clicker_org",
    "host_only",
    "own_thread_only",
    "unbound_confirmation",
]

EXTERNAL_REFUSAL = f"That's only for people in the {words_for('teams').space} that added Daimon."

type Refuse[R] = Callable[[str], R]


async def clicker_org(runtime: TeamsRuntime, activity: InvokeActivity) -> RequesterOrg:
    """Which organisation the clicker belongs to: HOST only on positive evidence.

    A 1:1 chat is never opened across organisations: `card_actor` refuses a click there
    that names a foreign tenant, so a 1:1 click is placed by its own fields alone.
    """
    teams = runtime.settings.teams
    if teams is None:
        return RequesterOrg.UNKNOWN
    ours = canonical_uuid(teams.tenant_id) or teams.tenant_id
    conversation = activity.conversation
    personal = conversation.conversation_type == "personal"
    org, _ = classify_sender(activity.from_, activity.channel_data, ours=ours, direct_chat=personal)
    if personal and org is RequesterOrg.UNKNOWN and canonical_uuid(conversation.tenant_id) == ours:
        return RequesterOrg.HOST
    user_id = canonical_uuid(activity.from_.aad_object_id)
    if org is not RequesterOrg.UNKNOWN or runtime.requester_orgs is None or user_id is None:
        return org
    parent = conversation.id.split(";", 1)[0]
    return (await runtime.requester_orgs.resolve(parent, user_id))[0]


def _outside_a_chat(org: RequesterOrg, activity: InvokeActivity) -> bool:
    # A 1:1 click is `card_actor`'s to refuse, in each handler's own words.
    return org is not RequesterOrg.HOST and activity.conversation.conversation_type != "personal"


def host_only[C: ActivityContext[Any], R](
    runtime: TeamsRuntime,
    handler: Callable[[C], Awaitable[R]],
    refuse: Refuse[R],
    *,
    applies: Callable[[C], bool] = lambda _ctx: True,
) -> Callable[[C], Awaitable[R]]:
    """`handler`, but a clicker outside the organisation (or unplaced) gets `refuse`'s answer.

    `applies` narrows the guard to the clicks that need it; the rest reach `handler` as is.
    """

    @functools.wraps(handler)
    async def guarded(ctx: C) -> R:
        if not applies(ctx):
            return await handler(ctx)
        if _outside_a_chat(await clicker_org(runtime, ctx.activity), ctx.activity):
            return refuse(EXTERNAL_REFUSAL)
        return await handler(ctx)

    return guarded


_REQUESTER_BOUND_OPS = frozenset({"approve", "deny", "details"})


def unbound_confirmation(ctx: ActivityContext[Any]) -> bool:
    """A confirmation click that is not requester-bound: `always` and anything unrecognised.

    Approve and Deny check the turn's requester themselves. `always` saves an agent-wide
    rule, so it needs a clicker placed as one of ours, as does any op not listed.
    """
    op = submitted_fields(ctx.activity.value.action.data).get("op")
    return op not in _REQUESTER_BOUND_OPS


def own_thread_only[C: ActivityContext[Any], R](
    runtime: TeamsRuntime, handler: Callable[[C], Awaitable[R]], refuse: Refuse[R]
) -> Callable[[C], Awaitable[R]]:
    """`handler`, but an outside clicker passes only in a thread where they asked.

    Their own live session in the clicked conversation stands in for "their own turn".
    """

    @functools.wraps(handler)
    async def guarded(ctx: C) -> R:
        if not _outside_a_chat(await clicker_org(runtime, ctx.activity), ctx.activity):
            return await handler(ctx)
        if await _asked_here(runtime, ctx.activity):
            return await handler(ctx)
        return refuse(EXTERNAL_REFUSAL)

    return guarded


async def _asked_here(runtime: TeamsRuntime, activity: InvokeActivity) -> bool:
    teams = runtime.settings.teams
    user_id = canonical_uuid(activity.from_.aad_object_id)
    if teams is None or user_id is None:
        return False
    tenant_id: uuid.UUID | None = await live_tenant_id(runtime.sessionmaker, teams.tenant_id)
    if tenant_id is None:
        return False
    async with runtime.sessionmaker() as session:
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="teams", external_id=user_id
        )
        if principal is None:
            return False
        live = await get_live_thread_session(
            session,
            tenant_id=tenant_id,
            platform="teams",
            thread_id=activity.conversation.id,
            account_id=principal.account_id,
        )
    return live is not None
