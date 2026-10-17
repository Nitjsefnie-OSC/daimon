"""replace_channel_file on Teams: a new version of a channel file, or a refusal that says why."""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._send import (
    _teams_replace_file_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.file_uploads import create_upload, store_upload_content
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_TEAM = "19:team@thread.tacv2"
_GROUP = "00000000-0000-4000-8000-0000000000aa"
_CHANNEL = "19:chan@thread.tacv2"
_THREAD = f"{_CHANNEL};messageid=1700000000000"
_LINK = "https://example.sharepoint.com/sites/Lab/Shared%20Documents/research/deck.pptx"
_WEB_URL = "https://example.sharepoint.com/sites/Lab/_layouts/15/Doc.aspx?file=deck.pptx"
_VERSION = '"{DECK},1"'


class _Fake:
    """Bot Framework and Graph for one team whose `research` channel holds `deck.pptx`.

    `folder` is the deck's parent folder: `f1` is the channel's, `f2` a sibling's.
    """

    def __init__(self, *, folder: str = "f1", put_status: int = 200, member: bool = True) -> None:
        self.folder, self.put_status, self.member = folder, put_status, member
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, path = request.url, request.url.path
        if url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path.endswith(f"/v3/teams/{_TEAM}/conversations"):
            channels = [{"id": _TEAM}, {"id": _CHANNEL, "name": "research", "type": "standard"}]
            return httpx.Response(200, json={"conversations": channels})
        if "/members/" in path:
            if not self.member:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": "29:x", "aadObjectId": _CALLER})
        if url.host != "graph.microsoft.com":
            return httpx.Response(404)
        if path.endswith("/filesFolder"):
            return httpx.Response(200, json={"id": "f1", "parentReference": {"driveId": "d1"}})
        if path.startswith("/v1.0/shares/u!"):
            return httpx.Response(200, json={"id": "deck", "parentReference": {"driveId": "d1"}})
        if path == "/v1.0/drives/d1/items/deck" and request.method == "GET":
            parent = {"driveId": "d1", "id": self.folder}
            item = {"id": "deck", "name": "deck.pptx", "eTag": _VERSION, "parentReference": parent}
            return httpx.Response(200, json=item)
        if path == "/v1.0/drives/d1/items/f2":
            return httpx.Response(200, json={"id": "f2", "parentReference": {"driveId": "d1"}})
        if path == "/v1.0/drives/d1/items/deck/content" and request.method == "PUT":
            if self.put_status != 200:
                return httpx.Response(self.put_status, json={"error": {"code": "refused"}})
            saved = {"id": "deck", "name": "deck.pptx", "eTag": '"{DECK},2"', "webUrl": _WEB_URL}
            return httpx.Response(200, json=saved)
        return httpx.Response(404)

    def writes(self) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method in ("PUT", "POST", "PATCH") and r.url.host == "graph.microsoft.com"
        ]

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/activities")]


def _runtime(fake: _Fake, sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    client = TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        client_id="app-id",
        client_secret="secret",
        tenant_id=_ENTRA,
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(),  # type: ignore[arg-type]  # unused by the Teams impls
        settings=MagicMock(),  # type: ignore[arg-type]  # unused by the Teams impls
        deployment_default=DeploymentDefault(),
        teams_client=client,
    )


async def _setup(db_session: AsyncSession, filename: str = "deck.pptx") -> tuple[AuthIdentity, str]:
    """The caller, and a handle holding the edited `filename`."""
    tenant = await make_tenant(db_session)
    await record_teams_installation(
        db_session, tenant_id=tenant.id, team_id=_TEAM, group_id=_GROUP, name="Lab"
    )
    now = dt.datetime.now(dt.UTC)
    row, token = await create_upload(
        db_session,
        tenant_id=tenant.id,
        title="deck",
        display_filename=filename,
        content_type="application/octet-stream",
        now=now,
    )
    await store_upload_content(db_session, upload_token=token, data=b"edited deck", now=now)
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=Role.USER,
        platform="teams",
        platform_user_id=_CALLER,
    )
    return auth, row.id


async def test_a_channel_file_gets_a_new_version_at_the_version_read(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """One conditional PUT over the item; the reply line links it and nothing is posted."""
    (auth, handle), fake = await _setup(db_session), _Fake()
    replaced = await _teams_replace_file_impl(
        _runtime(fake, sessionmaker),
        auth,
        channel_id=_THREAD,
        link=_LINK,
        version=_VERSION,
        file_handle=handle,
    )
    [put] = fake.writes()
    assert put.url.path == "/v1.0/drives/d1/items/deck/content", "the item itself, not a copy"
    assert put.headers["If-Match"] == _VERSION, "only while the file is at the version read"
    assert put.content == b"edited deck", "the staged bytes"
    assert replaced.version == '"{DECK},2"', "the new version, for a further replace"
    assert replaced.link == f"- [deck.pptx]({_WEB_URL})", "a reply line linking the file"
    assert fake.posts() == [], "the agent's reply carries the link; the tool posts nothing"


async def test_a_file_changed_since_it_was_read_is_refused_before_any_write(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake()
    with pytest.raises(ToolError, match="changed since you read it"):
        await _teams_replace_file_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_CHANNEL,
            link=_LINK,
            version='"{DECK},0"',
            file_handle=handle,
        )
    assert fake.writes() == [], "an older version is never written over a newer one"


@pytest.mark.parametrize(
    ("status", "match"),
    [
        (423, "locked, most likely open in a desktop Office app"),
        (412, "changed since you read it"),
        (403, "sensitivity label"),
        (500, "HTTP 500"),
    ],
)
async def test_a_refused_write_says_why_and_what_to_do(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    status: int,
    match: str,
) -> None:
    (auth, handle), fake = await _setup(db_session), _Fake(put_status=status)
    with pytest.raises(ToolError, match=match):
        await _teams_replace_file_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_CHANNEL,
            link=_LINK,
            version=_VERSION,
            file_handle=handle,
        )
    assert len(fake.writes()) == 1, f"HTTP {status}: one write, never retried"


async def test_a_file_outside_the_channels_folder_is_refused(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A file of the same library in another folder is not this channel's to change."""
    (auth, handle), fake = await _setup(db_session), _Fake(folder="f2")
    with pytest.raises(ToolError, match="not a file in this channel's Files folder"):
        await _teams_replace_file_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_CHANNEL,
            link=_LINK,
            version=_VERSION,
            file_handle=handle,
        )
    assert fake.writes() == [], "nothing written outside the channel's folder"


async def test_a_new_version_of_another_file_type_is_refused(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (auth, handle), fake = await _setup(db_session, "deck.pdf"), _Fake()
    with pytest.raises(ToolError, match="same type of file"):
        await _teams_replace_file_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=_CHANNEL,
            link=_LINK,
            version=_VERSION,
            file_handle=handle,
        )
    assert fake.writes() == [], "a .pptx never gets PDF bytes"


@pytest.mark.parametrize(
    ("channel_id", "member", "match"),
    [
        (_CHANNEL, False, "not a member"),
        ("a:chat-1", True, "only a file in a Teams channel"),
    ],
)
async def test_a_caller_who_cannot_post_there_or_a_chat_is_refused(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    channel_id: str,
    member: bool,
    match: str,
) -> None:
    """The same roster check as a post: the bot never writes for someone outside the channel."""
    (auth, handle), fake = await _setup(db_session), _Fake(member=member)
    with pytest.raises(ToolError, match=match):
        await _teams_replace_file_impl(
            _runtime(fake, sessionmaker),
            auth,
            channel_id=channel_id,
            link=_LINK,
            version=_VERSION,
            file_handle=handle,
        )
    graph = [r for r in fake.requests if r.url.host == "graph.microsoft.com"]
    assert graph == [], "refused before any Graph call"
