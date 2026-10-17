"""A team's SharePoint files through Graph, under the application permission `Files.ReadWrite.All`.

`Files.ReadWrite.All` reaches every file in the tenant, past SharePoint's
per-item permissions, so the channel's Files folder is the only boundary.
A channel's folder is `filesFolder`, standard, private and shared channels
alike. A shared file is found by its URL through `/shares`, then read again
by drive and id; its short-lived `downloadUrl` is pre-authorised, so only a
file inside the channel's folder, which the channel's members share, gets
one, never one from another site, library or folder. Inside means the
folder's id is among the file's parents' ids, which survive a rename and need
no case folding or percent-decoding. Uploads never overwrite
(`conflictBehavior=rename`) and go up in one `PUT`: upload sessions need
`Sites.ReadWrite.All` app-only. `replace` is the one write over an existing
file: a new version of an item found inside the folder, sent with the eTag it
was read at (`If-Match`), so a file changed since then is refused (412), not
overwritten; SharePoint's version history keeps the earlier ones. An
uploaded item's first-page thumbnail is a pre-authorised URL too, which its
caller fetches without a token. Any failure raises `GraphUnavailable`.
"""

from __future__ import annotations

import base64

import httpx
from daimon.core.teams_graph import (
    GRAPH_ROOT,
    GraphClient,
    GraphUnavailable,
    is_sharepoint_host,
    path_segment,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

# One PUT takes up to 250 MB; files are capped far below that (`MAX_BYTES_PER_FILE`, 20 MiB),
# which this allows at about 175 KB/s.
UPLOAD_TIMEOUT_S = 120.0
# A shared file's parents checked for the channel's folder, each past the first one read;
# a file deeper than that stays a name.
MAX_FOLDER_DEPTH = 8
_CONFLICT = "@microsoft.graph.conflictBehavior"


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _Parent(_Model):
    drive_id: str | None = None
    # The parent folder's item id; absent for a library's root.
    id: str | None = None


class DriveItem(_Model):
    """The fields of a Graph `driveItem` daimon reads."""

    id: str
    name: str | None = None
    web_url: str | None = None
    # Changes with every write to the item; what `replace` sends as `If-Match`.
    e_tag: str | None = None
    parent_reference: _Parent | None = None
    download_url: str | None = Field(default=None, alias="@microsoft.graph.downloadUrl")


class DriveFolder(_Model):
    drive_id: str
    item_id: str


class _Thumbnail(_Model):
    url: str


class _ThumbnailSet(_Model):
    large: _Thumbnail | None = None


class _ThumbnailSets(_Model):
    value: list[_ThumbnailSet] = Field(default_factory=list[_ThumbnailSet])


def _parse[T: BaseModel](model: type[T], data: object) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise GraphUnavailable("unexpected body", status=200) from err


class SharePoint:
    """Channel folders, uploads and shared-file lookups over `graph`."""

    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph

    async def channel_folder(self, group_id: str, channel_id: str) -> DriveFolder:
        """The channel's Files folder, on its own site for a private or shared channel."""
        channel = f"{GRAPH_ROOT}/teams/{path_segment(group_id)}/channels/{path_segment(channel_id)}"
        item = _parse(DriveItem, await self._graph.send("GET", f"{channel}/filesFolder"))
        if item.parent_reference is None or item.parent_reference.drive_id is None:
            raise GraphUnavailable("folder without a drive", status=200)
        return DriveFolder(drive_id=item.parent_reference.drive_id, item_id=item.id)

    async def upload(self, folder: DriveFolder, name: str, content: bytes) -> DriveItem:
        """Save `content` as `name` in `folder`, renamed rather than overwriting."""
        target = (
            f"{GRAPH_ROOT}/drives/{path_segment(folder.drive_id)}"
            f"/items/{path_segment(folder.item_id)}:/{path_segment(name)}:/content"
        )
        params = {_CONFLICT: "rename"}
        data = await self._graph.send(
            "PUT", target, params=params, content=content, timeout=UPLOAD_TIMEOUT_S
        )
        item = _parse(DriveItem, data)
        if item.parent_reference is None or item.parent_reference.drive_id is None:
            # Later calls on the item address it by drive, which the reply may leave out.
            parent = _Parent(drive_id=folder.drive_id)
            item = item.model_copy(update={"parent_reference": parent})
        return item

    async def thumbnail_url(self, item: DriveItem) -> str | None:
        """The pre-authorised URL of `item`'s large first-page thumbnail, if Graph has one."""
        if item.parent_reference is None or item.parent_reference.drive_id is None:
            raise GraphUnavailable("item without a drive")
        drive = path_segment(item.parent_reference.drive_id)
        data = await self._graph.send(
            "GET", f"{GRAPH_ROOT}/drives/{drive}/items/{path_segment(item.id)}/thumbnails"
        )
        sets = _parse(_ThumbnailSets, data).value
        return sets[0].large.url if sets and sets[0].large is not None else None

    async def replace(self, item: DriveItem, content: bytes, *, etag: str) -> DriveItem:
        """Write `content` over `item` as a new version, only while its eTag is still `etag`.

        Graph answers 412 when the file changed since `etag` was read, and 423 while
        it is locked, such as open in a desktop app; both raise `GraphUnavailable`.
        """
        if item.parent_reference is None or item.parent_reference.drive_id is None:
            raise GraphUnavailable("item without a drive")
        drive = item.parent_reference.drive_id
        target = f"{GRAPH_ROOT}/drives/{path_segment(drive)}/items/{path_segment(item.id)}/content"
        data = await self._graph.send(
            "PUT", target, content=content, headers={"If-Match": etag}, timeout=UPLOAD_TIMEOUT_S
        )
        saved = _parse(DriveItem, data)
        if saved.parent_reference is None or saved.parent_reference.drive_id is None:
            saved = saved.model_copy(update={"parent_reference": _Parent(drive_id=drive)})
        return saved

    async def download_url(self, content_url: str, *, folder: DriveFolder) -> str:
        """The pre-authorised download URL of the shared file at `content_url`, inside `folder`."""
        return download_url_of(await self.shared_item(content_url, folder=folder))

    async def shared_item(self, content_url: str, *, folder: DriveFolder) -> DriveItem:
        """The shared file at `content_url`, read by drive and id, only inside `folder`."""
        # Teams sends the path unencoded: a `#` or `?` there is part of the file's name.
        url = _url(content_url.replace("#", "%23").replace("?", "%3F"))
        parts = url.path.strip("/").split("/")
        if not is_sharepoint_host(url) or len(parts) < 3 or parts[0] not in ("sites", "teams"):
            raise GraphUnavailable("not a SharePoint site file")
        if {".", ".."} & set(parts):
            raise GraphUnavailable("a relative path segment")
        share = base64.urlsafe_b64encode(str(url).encode()).rstrip(b"=").decode()
        found = _parse(
            DriveItem, await self._graph.send("GET", f"{GRAPH_ROOT}/shares/u!{share}/driveItem")
        )
        if found.parent_reference is None or found.parent_reference.drive_id != folder.drive_id:
            raise GraphUnavailable("outside the channel's folder", status=200)
        # Read again by drive and id: the item, not the share's view of it, is checked.
        drive, item_id = path_segment(folder.drive_id), path_segment(found.id)
        data = await self._graph.send("GET", f"{GRAPH_ROOT}/drives/{drive}/items/{item_id}")
        item = _parse(DriveItem, data)
        # The permission reads past per-item permissions: a file outside the channel's
        # folder may be one its members cannot open, and the URL works for anyone.
        if not await self._is_inside(item, folder):
            raise GraphUnavailable("outside the channel's folder", status=200)
        return item

    async def _is_inside(self, item: DriveItem, folder: DriveFolder) -> bool:
        """Whether `folder` is among `item`'s parents, up to `MAX_FOLDER_DEPTH` levels."""
        drive, parent = path_segment(folder.drive_id), item.parent_reference
        for _ in range(MAX_FOLDER_DEPTH):
            if parent is None or parent.drive_id != folder.drive_id or parent.id is None:
                return False
            if parent.id == folder.item_id:
                return True
            data = await self._graph.send(
                "GET", f"{GRAPH_ROOT}/drives/{drive}/items/{path_segment(parent.id)}"
            )
            parent = _parse(DriveItem, data).parent_reference
        return False


def download_url_of(item: DriveItem) -> str:
    """`item`'s pre-authorised download URL; `GraphUnavailable` unless it is on SharePoint."""
    download = item.download_url
    if download is None or not is_sharepoint_host(_url(download)):
        raise GraphUnavailable("no SharePoint download URL", status=200)
    return download


def file_link(name: str, web_url: str | None) -> str:
    """A markdown list line for a saved file, linked only when its URL is on SharePoint."""
    label = name.replace("[", "\\[").replace("]", "\\]")
    if web_url is None or not is_sharepoint_host(httpx.URL(web_url)):
        return f"- {label}"
    return f"- [{label}]({web_url.replace('(', '%28').replace(')', '%29')})"


def _url(value: str) -> httpx.URL:
    """`value` parsed; Graph's URLs are untrusted, so a malformed one is unavailable."""
    try:
        return httpx.URL(value)
    except httpx.InvalidURL as err:
        raise GraphUnavailable("not a URL", status=200) from err
