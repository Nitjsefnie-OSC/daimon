"""Which organisation a Teams sender belongs to, and what core does with the answer.

Every payload here is synthetic: shaped from the documented Bot Framework and Graph
fields, not captured from a real cross-tenant conversation. Nothing in this file shows
that Teams puts these fields on the wire for a direct connect member; it shows that
the adapter reads whichever of them it is given fail-closed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from daimon.adapters.teams import billing_panel, card, requester_org
from daimon.adapters.teams.admissions import (
    _INBOUND,  # pyright: ignore[reportPrivateUsage]
    stored_payload,
)
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.external_clicks import EXTERNAL_REFUSAL
from daimon.adapters.teams.identity import (
    GraphSource,
    TeamsInbound,
    channel_shared_of,
    classify_requester,
    parse_graph_message,
    parse_inbound,
)
from daimon.adapters.teams.requester_org import (
    FOUND_TTL_S,
    MISSED_TTL_S,
    MemberFacts,
    RequesterOrgResolver,
    SdkMemberRead,
    verdict_of,
)
from daimon.adapters.teams.tool_confirmation import VERB as TOOL_CONFIRM_VERB
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.teams_graph import GraphMessage
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_hold import RequesterOrg
from daimon.testing import build_fake_anthropic, ma_session
from daimon.testing.ma_models import DEFAULT_AGENT_NAME, DEFAULT_ENV_NAME
from daimon.testing.turn_router import AGENT_ID, ENV_ID, build_turn_router
from microsoft_teams.api import MessageActivity
from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass as pydantic_dataclass
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    THREAD_ID,
    FakeSender,
    TeamsApiFake,
    bot_token,
    build_teams_runtime,
    finish_turns,
    make_card_action,
    make_channel_activity,
    make_group_activity,
    make_inbound,
    make_invoke,
    make_message_activity,
    post_activity,
    running_service,
)

OTHER_TENANT = str(uuid.UUID(int=99))
THIRD_TENANT = str(uuid.UUID(int=98))
HOST, OTHER, UNKNOWN = RequesterOrg.HOST, RequesterOrg.OTHER, RequesterOrg.UNKNOWN
OURS_UPPER = ENTRA_TENANT_ID.upper()

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")


# --- the pure classifier ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence", "channel_type", "direct", "expected"),
    [
        # Positive evidence and an explicit channel type that cannot hold outsiders: HOST.
        ([ENTRA_TENANT_ID], "standard", False, (HOST, None)),
        ([ENTRA_TENANT_ID], "private", False, (HOST, None)),
        ([OURS_UPPER], " Standard ", False, (HOST, None)),
        ([None, ENTRA_TENANT_ID, ENTRA_TENANT_ID], "standard", False, (HOST, None)),
        # A 1:1 chat is never opened across organisations.
        ([ENTRA_TENANT_ID], None, True, (HOST, None)),
        # Ours but the channel may hold an outsider, or says nothing: UNKNOWN.
        ([ENTRA_TENANT_ID], "shared", False, (UNKNOWN, None)),
        ([ENTRA_TENANT_ID], None, False, (UNKNOWN, None)),
        ([ENTRA_TENANT_ID], "", False, (UNKNOWN, None)),
        ([ENTRA_TENANT_ID], "somethingnew", False, (UNKNOWN, None)),
        # No tenant field at all is no evidence, even in a standard channel.
        ([], "standard", False, (UNKNOWN, None)),
        ([None, "", "  "], "standard", False, (UNKNOWN, None)),
        # Any field naming another tenant is OTHER, whatever else says ours.
        ([OTHER_TENANT], "standard", False, (OTHER, OTHER_TENANT)),
        ([OTHER_TENANT], "shared", False, (OTHER, OTHER_TENANT)),
        ([OTHER_TENANT], None, False, (OTHER, OTHER_TENANT)),
        ([ENTRA_TENANT_ID, OTHER_TENANT], "standard", False, (OTHER, OTHER_TENANT)),
        ([OTHER_TENANT, ENTRA_TENANT_ID], None, True, (OTHER, OTHER_TENANT)),
        ([OTHER_TENANT.upper()], "private", False, (OTHER, OTHER_TENANT)),
        # A value that is not a UUID is not ours either.
        (["contoso"], "standard", False, (OTHER, "contoso")),
        ([" contoso "], None, False, (OTHER, "contoso")),
        # A value that cannot be read blocks HOST but names no other tenant.
        ([ENTRA_TENANT_ID, 7], "standard", False, (UNKNOWN, None)),
        ([7], "standard", False, (UNKNOWN, None)),
    ],
)
def test_classifier_truth_table(
    evidence: list[object],
    channel_type: str | None,
    direct: bool,
    expected: tuple[RequesterOrg, str | None],
) -> None:
    assert (
        classify_requester(
            evidence, ours=ENTRA_TENANT_ID, channel_type=channel_type, direct_chat=direct
        )
        == expected
    )


@pytest.mark.parametrize(
    ("channel_type", "expected"),
    [
        ("shared", True),
        (" SHARED ", True),
        ("standard", False),
        ("private", False),
        (None, None),
        ("", None),
        ("unheard-of", None),
    ],
)
def test_only_an_explicit_type_decides_whether_a_channel_is_shared(
    channel_type: str | None, expected: bool | None
) -> None:
    assert channel_shared_of(channel_type) is expected


# --- what an activity or a Graph message reduces to -------------------------------------


def _parse(payload: dict[str, object]) -> TeamsInbound:
    inbound = parse_inbound(
        MessageActivity.model_validate(payload),
        configured_tenant=ENTRA_TENANT_ID,
        service_url=None,
    )
    assert isinstance(inbound, TeamsInbound), inbound
    return inbound


def test_a_standard_channel_member_of_ours_is_host_and_the_channel_not_shared() -> None:
    inbound = _parse(make_channel_activity(channel_type="standard"))
    assert (inbound.requester_org, inbound.channel_shared) == (HOST, False)


def test_a_shared_channel_never_gives_host_from_the_activity_alone() -> None:
    inbound = _parse(make_channel_activity(channel_type="shared"))
    assert (inbound.requester_org, inbound.channel_shared) == (UNKNOWN, True)


def test_a_channel_with_no_type_is_not_assumed_standard() -> None:
    inbound = _parse(make_channel_activity())
    assert (inbound.requester_org, inbound.channel_shared) == (UNKNOWN, None)


def test_a_sender_tenant_other_than_ours_is_other_with_its_home_in_every_field() -> None:
    inbound = _parse(make_channel_activity(channel_type="standard", from_tenant_id=OTHER_TENANT))
    assert (inbound.requester_org, inbound.home_tenant_id) == (OTHER, OTHER_TENANT)
    assert inbound.is_foreign, "wording that promises another organisation applies"


def test_a_group_chat_sender_is_unknown_until_looked_up() -> None:
    inbound = _parse(make_group_activity())
    assert inbound.requester_org is UNKNOWN and not inbound.is_foreign


def test_a_personal_chat_is_host() -> None:
    assert _parse(make_message_activity()).requester_org is HOST


def _graph(tenant: str | None) -> GraphMessage:
    user: dict[str, Any] = {"id": AAD_OBJECT_ID, "displayName": "Ada"}
    if tenant is not None:
        user["tenantId"] = tenant
    return GraphMessage.model_validate(
        {
            "id": "1700000000001",
            "messageType": "message",
            "createdDateTime": "2026-01-01T00:00:00Z",
            "from": {"user": user},
            "body": {"contentType": "html", "content": "<p>hello</p>"},
        }
    )


@pytest.mark.parametrize(
    ("tenant", "channel_type", "expected", "shared"),
    [
        (ENTRA_TENANT_ID, "private", HOST, False),
        (ENTRA_TENANT_ID, "standard", HOST, False),
        (ENTRA_TENANT_ID, "shared", UNKNOWN, True),
        (ENTRA_TENANT_ID, None, UNKNOWN, None),
        (None, "private", UNKNOWN, False),
        (OTHER_TENANT, "shared", OTHER, True),
        (OTHER_TENANT, None, OTHER, None),
    ],
)
def test_a_graph_message_is_classified_by_its_sender_tenant_and_the_source_type(
    tenant: str | None,
    channel_type: str | None,
    expected: RequesterOrg,
    shared: bool | None,
) -> None:
    source = GraphSource(kind="channel", channel_id=CHANNEL_ID, channel_type=channel_type)
    inbound = parse_graph_message(
        _graph(tenant),
        source,
        configured_tenant=ENTRA_TENANT_ID,
        bot_app_id="bot",
        mentions=True,
    )
    # An unaddressed root post is refused; an at-mention-free channel reply is not a turn.
    if not isinstance(inbound, TeamsInbound):
        inbound = parse_graph_message(
            _graph(tenant).model_copy(update={"reply_to_id": "1699999999999"}),
            source,
            configured_tenant=ENTRA_TENANT_ID,
            bot_app_id="bot",
        )
    assert isinstance(inbound, TeamsInbound), inbound
    assert (inbound.requester_org, inbound.channel_shared) == (expected, shared)


def test_a_stored_message_written_before_the_field_existed_replays_fail_closed() -> None:
    stored = _INBOUND.dump_python(
        _parse(make_channel_activity(channel_type="standard")), mode="json"
    )
    assert stored["requester_org"] == "host"
    for gone in ("requester_org", "channel_shared"):
        del stored[gone]
    stored["is_foreign"] = False  # the field this one replaces
    replayed = _INBOUND.validate_python(stored)
    assert replayed.requester_org is UNKNOWN, "never HOST by default"
    assert replayed.channel_shared is None


# --- the member lookup -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (MemberFacts(AAD_OBJECT_ID, ENTRA_TENANT_ID, "user"), (HOST, None)),
        (MemberFacts(AAD_OBJECT_ID.upper(), OURS_UPPER, None), (HOST, None)),
        (MemberFacts(AAD_OBJECT_ID, ENTRA_TENANT_ID, "guest"), (HOST, None)),
        (MemberFacts(AAD_OBJECT_ID, ENTRA_TENANT_ID, "anonymous"), (UNKNOWN, None)),
        (MemberFacts(AAD_OBJECT_ID, ENTRA_TENANT_ID, " Anonymous "), (UNKNOWN, None)),
        (MemberFacts(AAD_OBJECT_ID, OTHER_TENANT, "user"), (OTHER, OTHER_TENANT)),
        (MemberFacts(AAD_OBJECT_ID, OTHER_TENANT, "anonymous"), (OTHER, OTHER_TENANT)),
        (MemberFacts(AAD_OBJECT_ID, None, "user"), (UNKNOWN, None)),
        (MemberFacts(AAD_OBJECT_ID, "  ", "user"), (UNKNOWN, None)),
        (MemberFacts(OTHER_AAD_OBJECT_ID, ENTRA_TENANT_ID, "user"), (UNKNOWN, None)),
        (MemberFacts(None, ENTRA_TENANT_ID, "user"), (UNKNOWN, None)),
        (None, (UNKNOWN, None)),
    ],
)
def test_a_member_read_proves_host_only_for_the_same_person_in_our_tenant(
    facts: MemberFacts | None, expected: tuple[RequesterOrg, str | None]
) -> None:
    assert verdict_of(facts, ours=ENTRA_TENANT_ID, aad_object_id=AAD_OBJECT_ID) == expected


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _resolver(
    read: Callable[[str, str], Awaitable[MemberFacts | None]], clock: _Clock
) -> RequesterOrgResolver:
    return RequesterOrgResolver(read, ours=ENTRA_TENANT_ID, clock=clock)  # pyright: ignore[reportArgumentType]


async def test_a_settled_answer_is_reused_for_minutes_and_then_read_again() -> None:
    calls = AsyncMock(return_value=MemberFacts(AAD_OBJECT_ID, OTHER_TENANT, "user"))
    clock = _Clock()
    resolver = _resolver(calls, clock)
    for _ in range(3):
        assert await resolver.resolve("19:c", AAD_OBJECT_ID) == (OTHER, OTHER_TENANT)
    assert calls.await_count == 1
    await resolver.resolve("19:other-channel", AAD_OBJECT_ID)
    assert calls.await_count == 2, "the cache key is (conversation, user)"
    clock.now += FOUND_TTL_S + 1
    await resolver.resolve("19:c", AAD_OBJECT_ID)
    assert calls.await_count == 3


async def test_a_failure_is_remembered_only_briefly_and_then_retried() -> None:
    calls = AsyncMock(side_effect=[TimeoutError(), httpx.ConnectError("down"), None, None])
    clock = _Clock()
    resolver = _resolver(calls, clock)
    assert await resolver.resolve("19:c", AAD_OBJECT_ID) == (UNKNOWN, None)
    assert await resolver.resolve("19:c", AAD_OBJECT_ID) == (UNKNOWN, None)
    assert calls.await_count == 1, "a burst reuses the failure"
    clock.now += MISSED_TTL_S + 1
    assert MISSED_TTL_S < FOUND_TTL_S
    assert await resolver.resolve("19:c", AAD_OBJECT_ID) == (UNKNOWN, None)
    assert calls.await_count == 2, "then it asks again"


async def test_with_no_member_read_every_sender_stays_unknown() -> None:
    resolver = RequesterOrgResolver(None, ours=ENTRA_TENANT_ID)
    assert await resolver.resolve("19:c", AAD_OBJECT_ID) == (UNKNOWN, None)


async def _lookup(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    user: str = AAD_OBJECT_ID,
) -> tuple[RequesterOrg, str | None]:
    async with running_service(build_teams_runtime(db_session_factory), fake) as service:
        resolver = RequesterOrgResolver(SdkMemberRead(service.teams_app), ours=ENTRA_TENANT_ID)
        return await resolver.resolve(CHANNEL_ID, user)


async def test_the_sdk_read_places_a_member_of_ours(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await _lookup(db_session_factory, TeamsApiFake()) == (HOST, None)


async def test_the_sdk_read_places_a_guest_of_ours_as_host(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake(roles={AAD_OBJECT_ID: "guest"})
    assert await _lookup(db_session_factory, fake) == (HOST, None)


async def test_the_sdk_read_places_a_member_of_another_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake(foreign_members={AAD_OBJECT_ID: OTHER_TENANT})
    assert await _lookup(db_session_factory, fake) == (OTHER, OTHER_TENANT)


@pytest.mark.parametrize(
    "fake",
    [
        TeamsApiFake(roles={AAD_OBJECT_ID: "anonymous"}),
        TeamsApiFake(absent={AAD_OBJECT_ID}),
        TeamsApiFake(failing_members={AAD_OBJECT_ID}),
        TeamsApiFake(tenantless={AAD_OBJECT_ID}),
    ],
    ids=["anonymous", "not-found", "server-error", "no-tenant-id"],
)
async def test_the_sdk_read_leaves_everyone_else_unknown(
    db_session_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake
) -> None:
    assert await _lookup(db_session_factory, fake) == (UNKNOWN, None)


async def test_a_member_read_that_hangs_times_out_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hang(conversation_id: str, aad_object_id: str) -> None:
        await asyncio.sleep(60)

    conversations = SimpleNamespace(get_member_by_id=hang)
    app = SimpleNamespace(
        api=SimpleNamespace(
            from_service_url=lambda _url: SimpleNamespace(conversations=conversations)  # pyright: ignore[reportUnknownLambdaType]
        )
    )
    monkeypatch.setattr(requester_org, "SEND_TIMEOUT_S", 0.01)
    resolver = RequesterOrgResolver(SdkMemberRead(app), ours=ENTRA_TENANT_ID)  # pyright: ignore[reportArgumentType]
    assert await resolver.resolve(CHANNEL_ID, AAD_OBJECT_ID) == (UNKNOWN, None)


# --- what admission receives ------------------------------------------------------------


class _Admissions:
    """Wraps `admit`: records what the Teams adapter passed and what core decided."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], Admission]] = []

    async def __call__(self, deps: Any, **kwargs: Any) -> Admission:
        admission = await admit(deps, **kwargs)
        self.calls.append((kwargs, admission))
        return admission


async def _turn(
    db_session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, object],
    fake: TeamsApiFake | None = None,
) -> _Admissions:
    await provision_tenant(
        db_session_factory,
        platform="teams",
        workspace_id=ENTRA_TENANT_ID,
        signup_credit=Decimal("100"),
    )
    tenant = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    session_id = "sesn_requester_org"
    router = build_turn_router(str(tenant), session_id=session_id)
    runtime = build_teams_runtime(
        db_session_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        deployment_default=DeploymentDefault(
            agent_name=DEFAULT_AGENT_NAME, environment_name=DEFAULT_ENV_NAME
        ),
    )
    session = ma_session(id=session_id, agent_id=AGENT_ID, environment_id=ENV_ID)
    admissions = _Admissions()
    with (
        patch("daimon.core.turn.prepare.create_session", return_value=session),
        patch("daimon.adapters.teams.app.admit", new=admissions),
    ):
        async with running_service(runtime, fake or TeamsApiFake()) as service:
            await post_activity(service, payload)
            await finish_turns(service.turns)
    return admissions


async def test_an_internal_member_in_a_standard_channel_is_unheld(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = make_channel_activity(channel_type="standard", from_tenant_id=ENTRA_TENANT_ID)
    [(kwargs, admission)] = (await _turn(db_session_factory, payload)).calls
    assert (kwargs["requester_org"], kwargs["channel_shared"]) == (HOST, False)
    assert admission.hold is None and not admission.shared_channel


async def test_an_internal_member_in_a_shared_channel_is_held_but_not_a_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = make_channel_activity(channel_type="shared")  # settled by the roster: ours
    [(kwargs, admission)] = (await _turn(db_session_factory, payload)).calls
    assert (kwargs["requester_org"], kwargs["channel_shared"]) == (HOST, True)
    assert admission.hold is not None and not admission.hold.client
    assert admission.hold.channel_id == CHANNEL_ID and admission.shared_channel


async def test_a_sender_of_another_organisation_in_a_shared_channel_is_a_held_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = make_channel_activity(channel_type="shared", from_tenant_id=OTHER_TENANT)
    [(kwargs, admission)] = (await _turn(db_session_factory, payload)).calls
    assert (kwargs["requester_org"], kwargs["requester_home_id"]) == (OTHER, OTHER_TENANT)
    assert kwargs["channel_shared"] is True
    assert admission.hold is not None and admission.hold.client


async def test_a_sender_the_roster_places_elsewhere_is_other_with_no_tenant_on_the_activity(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake(foreign_members={AAD_OBJECT_ID: OTHER_TENANT})
    [(kwargs, admission)] = (await _turn(db_session_factory, make_channel_activity(), fake)).calls
    assert (kwargs["requester_org"], kwargs["requester_home_id"]) == (OTHER, OTHER_TENANT)
    assert admission.hold is not None and admission.hold.client


@pytest.mark.parametrize(
    "fake",
    [
        TeamsApiFake(absent={AAD_OBJECT_ID}),
        TeamsApiFake(failing_members={AAD_OBJECT_ID}),
        TeamsApiFake(tenantless={AAD_OBJECT_ID}),
    ],
    ids=["not-found", "server-error", "no-tenant-id"],
)
async def test_a_sender_nothing_can_place_is_unknown_and_held_as_a_client_for_the_turn(
    db_session_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake
) -> None:
    [(kwargs, admission)] = (await _turn(db_session_factory, make_channel_activity(), fake)).calls
    assert (kwargs["requester_org"], kwargs["requester_home_id"]) == (UNKNOWN, None)
    assert admission.hold is not None and admission.hold.client


async def test_a_direct_chat_turn_is_host_with_no_lookup(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake()
    [(kwargs, admission)] = (await _turn(db_session_factory, make_message_activity(), fake)).calls
    assert kwargs["requester_org"] is HOST and admission.hold is None
    assert not [r for r in fake.requests if "/members/" in r.url], "no roster read was needed"


# --- continuations ---------------------------------------------------------------------


def _row(*, thread: str, parent: str) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        reason="wake",
        thread_id=thread,
        parent_channel_id=parent,
        requester_external_user_id=AAD_OBJECT_ID,
    )


async def _continuation(
    db_session_factory: async_sessionmaker[AsyncSession], row: Any, fake: TeamsApiFake
) -> dict[str, Any]:
    """What admission is told about a queued turn (admission itself refuses)."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    runtime = build_teams_runtime(db_session_factory)
    refused = AsyncMock(side_effect=AdmissionDenied(reason="writers_none"))
    async with running_service(runtime, fake) as service:
        with patch("daimon.adapters.teams.app.admit", refused), pytest.raises(AdmissionDenied):
            await service.turns._run_continuation(  # pyright: ignore[reportPrivateUsage]
                row,
                "seed",
                derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID),
                None,
            )
    [call] = refused.await_args_list
    return dict(call.kwargs)


async def test_a_channel_continuation_is_classified_by_the_roster_read_of_its_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = _row(thread=THREAD_ID, parent=CHANNEL_ID)
    assert (await _continuation(db_session_factory, row, TeamsApiFake()))["requester_org"] is HOST
    elsewhere = TeamsApiFake(foreign_members={AAD_OBJECT_ID: OTHER_TENANT})
    theirs = await _continuation(db_session_factory, row, elsewhere)
    assert (theirs["requester_org"], theirs["requester_home_id"]) == (OTHER, OTHER_TENANT)


async def test_a_continuation_the_roster_cannot_place_stays_unknown_never_host(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = _row(thread=THREAD_ID, parent=CHANNEL_ID)
    failing = TeamsApiFake(failing_members={AAD_OBJECT_ID})
    assert (await _continuation(db_session_factory, row, failing))["requester_org"] is UNKNOWN


async def test_a_one_to_one_continuation_is_host_without_a_lookup(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake(failing_members={AAD_OBJECT_ID})
    row = _row(thread="a:conversation-1", parent="a:conversation-1")
    admitted = await _continuation(db_session_factory, row, fake)
    assert admitted["requester_org"] is HOST and admitted["hold_unknown_place"] is False


# --- clicks from outside the organisation -------------------------------------------------


def _click(
    verb: str, op: str, data: dict[str, Any] | None = None, **invoke: Any
) -> dict[str, object]:
    invoke = {"conversation_type": "channel", "chat": THREAD_ID} | invoke
    return make_card_action(verb, op, invoke=invoke, **(data or {}))


async def _clicked(
    db_session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, object],
    fake: TeamsApiFake | None = None,
) -> Any:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with running_service(
        build_teams_runtime(db_session_factory), fake or TeamsApiFake()
    ) as svc:
        return await post_activity(svc, payload)


@pytest.mark.parametrize(
    "invoke",
    [
        {"from_tenant_id": OTHER_TENANT, "channel_type": "shared"},
        {"from_tenant_id": OTHER_TENANT},
    ],
    ids=["tenant-on-the-click", "tenant-only"],
)
async def test_an_outside_clicker_is_refused_every_control_beyond_the_requester_bound_ones(
    db_session_factory: async_sessionmaker[AsyncSession], invoke: dict[str, Any]
) -> None:
    answer = await _clicked(db_session_factory, _click(billing_panel.VERB, "refresh", **invoke))
    assert answer["value"] == EXTERNAL_REFUSAL
    assert EXTERNAL_REFUSAL == "That's only for people in the organisation that added Daimon."


async def test_a_clicker_the_roster_places_elsewhere_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake(foreign_members={AAD_OBJECT_ID: OTHER_TENANT})
    answer = await _clicked(
        db_session_factory, _click(billing_panel.VERB, "refresh", channel_type="shared"), fake
    )
    assert answer["value"] == EXTERNAL_REFUSAL


async def test_a_clicker_nothing_can_place_is_refused_too(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake(failing_members={AAD_OBJECT_ID})
    answer = await _clicked(
        db_session_factory, _click(billing_panel.VERB, "refresh", channel_type="shared"), fake
    )
    assert answer["value"] == EXTERNAL_REFUSAL


async def test_a_member_in_a_shared_channel_is_not_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    answer = await _clicked(
        db_session_factory, _click(billing_panel.VERB, "refresh", channel_type="shared")
    )
    assert EXTERNAL_REFUSAL not in str(answer)


async def test_an_outside_clicker_still_reaches_the_requester_bound_stop_and_approve(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    outside: dict[str, Any] = {"from_tenant_id": OTHER_TENANT, "channel_type": "shared"}
    stop = await _clicked(db_session_factory, _click(card.CANCEL_VERB, "stop", **outside))
    assert EXTERNAL_REFUSAL not in str(stop), "the handler checks the turn's author itself"
    approve = await _clicked(
        db_session_factory, _click(TOOL_CONFIRM_VERB, "approve", {"token": "t"}, **outside)
    )
    assert EXTERNAL_REFUSAL not in str(approve)


async def test_an_outside_clicker_cannot_vote_on_an_answer_in_a_thread_they_did_not_ask_in(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = make_invoke(
        "task/fetch",
        {"data": {"dialog_id": card.VOTE_UP_DIALOG}},
        conversation_type="channel",
        chat=THREAD_ID,
        from_tenant_id=OTHER_TENANT,
        channel_type="shared",
    )
    answer = await _clicked(db_session_factory, payload)
    assert EXTERNAL_REFUSAL in str(answer)


async def test_a_channel_continuation_whose_sharing_is_unknown_is_held_for_that_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = _row(thread=THREAD_ID, parent=CHANNEL_ID)
    admitted = await _continuation(db_session_factory, row, TeamsApiFake())
    assert admitted["requester_org"] is HOST, "the roster confirms the member"
    assert admitted["channel_shared"] is None and admitted["hold_unknown_place"] is True


# --- sharing is settled apart from who is asking ----------------------------------------


async def test_a_roster_confirmed_member_in_a_channel_of_unknown_type_is_still_held(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.shared_channels import is_channel_shared

    [(kwargs, admission)] = (await _turn(db_session_factory, make_channel_activity())).calls
    assert kwargs["requester_org"] is HOST and kwargs["channel_shared"] is None
    assert kwargs["hold_unknown_place"] is True
    assert admission.hold is not None and not admission.hold.client
    assert admission.shared_channel is False, "held, but never claimed to be shared"
    async with db_session_factory() as session:
        tenant = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
        assert not await is_channel_shared(
            session, tenant_id=tenant, platform="teams", channel_id=CHANNEL_ID
        ), "nothing was recorded"


async def test_a_channel_the_team_listing_types_standard_is_not_held(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake(channels={CHANNEL_ID: "Ops"})
    [(kwargs, admission)] = (await _turn(db_session_factory, make_channel_activity(), fake)).calls
    assert (kwargs["channel_shared"], kwargs["hold_unknown_place"]) == (False, False)
    assert admission.hold is None


async def test_a_channel_the_team_listing_types_shared_is_held_and_recorded(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = TeamsApiFake(channels={CHANNEL_ID: "Ops"}, channel_types={CHANNEL_ID: "shared"})
    [(kwargs, admission)] = (await _turn(db_session_factory, make_channel_activity(), fake)).calls
    assert kwargs["channel_shared"] is True
    assert admission.hold is not None and admission.shared_channel is True


# --- commands are placed before they run -------------------------------------------------


class _Direct:
    def __init__(self) -> None:
        self.looked_up: list[tuple[str, str]] = []

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        self.looked_up.append((conversation_id, aad_object_id))
        return "29:member"

    async def open_chat(self, member_id: str) -> str:
        return "a:direct"

    async def post(self, conversation_id: str, text: str) -> str:
        raise AssertionError("commands answer through the sender")


def _command_app(
    db_session_factory: async_sessionmaker[AsyncSession],
    commands: dict[str, Any],
    direct: _Direct,
    sender: FakeSender,
    verdict: tuple[RequesterOrg, str | None] | None = None,
) -> TeamsApp:
    runtime = build_teams_runtime(db_session_factory)
    if verdict is not None:
        read = AsyncMock(
            return_value=MemberFacts(AAD_OBJECT_ID, verdict[1] or ENTRA_TENANT_ID, "user")
        )
        runtime = dataclasses.replace(
            runtime, requester_orgs=RequesterOrgResolver(read, ours=ENTRA_TENANT_ID)
        )
    return TeamsApp(
        runtime=runtime,
        sender=sender,
        commands=commands,
        bot_token=bot_token,
        direct=direct,  # pyright: ignore[reportArgumentType]
    )


def _typed(command: str, org: RequesterOrg) -> TeamsInbound:
    return dataclasses.replace(
        make_inbound(command, conversation=THREAD_ID, kind="channel"),
        channel_id=CHANNEL_ID,
        requester_org=org,
    )


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize("org", [OTHER, UNKNOWN])
@pytest.mark.parametrize("command", ["memory", "setup", "billing", "routines", "privacy", "here"])
async def test_a_command_from_outside_or_unplaced_runs_nothing_and_opens_no_chat(
    db_session_factory: async_sessionmaker[AsyncSession], org: RequesterOrg, command: str
) -> None:
    sender, direct, run = FakeSender(), _Direct(), AsyncMock()
    teams = _command_app(db_session_factory, {command: run}, direct, sender)
    await teams._handle(_typed(command, org))  # pyright: ignore[reportPrivateUsage]
    assert run.await_count == 0, "no host data is read"
    assert direct.looked_up == [], "no 1:1 chat is attempted"
    [(_, reply, _)] = sender.sent
    assert reply.text == EXTERNAL_REFUSAL


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_memory_path_in_a_targeted_reply_from_an_unplaced_sender_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender, direct, run = FakeSender(), _Direct(), AsyncMock()
    teams = _command_app(db_session_factory, {"memory": run}, direct, sender)
    inbound = dataclasses.replace(_typed("memory /notes.md", UNKNOWN), is_targeted=True)
    await teams._handle(inbound)  # pyright: ignore[reportPrivateUsage]
    assert run.await_count == 0 and direct.looked_up == []


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_command_is_placed_by_the_roster_before_it_runs(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for verdict, runs in (((HOST, None), True), ((OTHER, OTHER_TENANT), False)):
        sender, direct, run = FakeSender(), _Direct(), AsyncMock()
        teams = _command_app(db_session_factory, {"memory": run}, direct, sender, verdict)
        await teams._handle(_typed("memory", UNKNOWN))  # pyright: ignore[reportPrivateUsage]
        [(_, reply, _)] = sender.sent
        assert (reply.text != EXTERNAL_REFUSAL) is runs, (verdict, reply.text)
        assert (direct.looked_up != []) is runs


@pytest.mark.usefixtures("provisioned_tenant")
async def test_help_stays_open_to_everyone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender, direct, run = FakeSender(), _Direct(), AsyncMock()
    teams = _command_app(db_session_factory, {"help": run}, direct, sender)
    inbound = dataclasses.replace(_typed("help", UNKNOWN), sender_id="29:asker")
    await teams._handle(inbound)  # pyright: ignore[reportPrivateUsage]
    assert run.await_count == 1, "answered privately in the thread"
    assert direct.looked_up == [], "and with no 1:1 chat opened for them"


# --- tool confirmation: only the requester-bound clicks are open ------------------------


@pytest.mark.parametrize(
    "invoke",
    [
        {"from_tenant_id": OTHER_TENANT, "channel_type": "shared"},
        {"channel_type": "shared"},
    ],
    ids=["outside", "unplaced"],
)
async def test_always_allow_needs_a_clicker_placed_as_ours(
    db_session_factory: async_sessionmaker[AsyncSession], invoke: dict[str, Any]
) -> None:
    fake = TeamsApiFake(failing_members={AAD_OBJECT_ID})
    answer = await _clicked(
        db_session_factory, _click(TOOL_CONFIRM_VERB, "always", {"token": "t"}, **invoke), fake
    )
    assert answer["value"] == EXTERNAL_REFUSAL


async def test_always_allow_from_a_placed_member_reaches_the_handler(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    answer = await _clicked(
        db_session_factory,
        _click(TOOL_CONFIRM_VERB, "always", {"token": "t"}, channel_type="shared"),
    )
    assert EXTERNAL_REFUSAL not in str(answer)


async def test_an_unrecognised_confirmation_op_from_outside_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    outside: dict[str, Any] = {"from_tenant_id": OTHER_TENANT, "channel_type": "shared"}
    answer = await _clicked(
        db_session_factory, _click(TOOL_CONFIRM_VERB, "remember", {"token": "t"}, **outside)
    )
    assert answer["value"] == EXTERNAL_REFUSAL


# --- rollback: the previous build reads a stored `is_foreign` ----------------------------


def _old_decoder() -> TypeAdapter[Any]:
    """The previous build's `TeamsInbound`: the same fields, `is_foreign` stored, no org."""
    new = {f.name for f in dataclasses.fields(TeamsInbound)} - {"requester_org", "channel_shared"}
    fields: list[tuple[str, Any, Any]] = [
        (name, Any, dataclasses.field(default=None)) for name in sorted(new)
    ]
    fields.append(("is_foreign", bool, dataclasses.field(default=False)))
    return TypeAdapter(pydantic_dataclass(dataclasses.make_dataclass("OldInbound", fields)))


@pytest.mark.parametrize(("org", "old_reads"), [(HOST, False), (OTHER, True), (UNKNOWN, True)])
def test_a_pending_payload_reads_as_foreign_to_the_build_a_rollback_returns_to(
    org: RequesterOrg, old_reads: bool
) -> None:
    inbound = dataclasses.replace(
        _parse(make_channel_activity(channel_type="shared", from_tenant_id=OTHER_TENANT)),
        requester_org=org,
        home_tenant_id=OTHER_TENANT if org is OTHER else None,
    )
    old = _old_decoder().validate_python(stored_payload(inbound))
    assert old.is_foreign is old_reads, "an unplaced or outside sender is never unrestricted"
    assert _INBOUND.validate_python(stored_payload(inbound)).requester_org is org
