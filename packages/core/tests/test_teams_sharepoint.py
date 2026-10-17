"""Which shared files count as inside a channel's Files folder: by their parents' ids."""

from __future__ import annotations

import base64

import httpx
import pytest
from daimon.core.teams_graph import GraphClient, GraphUnavailable
from daimon.core.teams_sharepoint import MAX_FOLDER_DEPTH, DriveFolder, DriveItem, SharePoint

_FOLDER = DriveFolder(drive_id="b!d", item_id="general")
_LIBRARY = "https://example.sharepoint.com/sites/team/Shared%20Documents"
_DOWNLOAD = "https://example.sharepoint.com/sites/team/_layouts/15/download.aspx?tempauth=x"
# Folder id -> its parent's id in drive `b!d`; the library's root has none.
_TREE: dict[str, str | None] = {
    "root": None,
    "general": "root",
    "q3": "general",
    "general-2": "root",
    "board": "root",
    "level-1": "general",
    **{f"level-{n}": f"level-{n - 1}" for n in range(2, MAX_FOLDER_DEPTH + 1)},
}


def _sharepoint(seen: list[str]) -> SharePoint:
    """Graph over `_TREE`; a file at `{_LIBRARY}/{folder id}/x` is `file-in-{folder id}`.

    Every file's `parentReference.path` claims the channel's folder, so only ids can tell.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path.startswith("/v1.0/shares/u!"):
            share = path.removeprefix("/v1.0/shares/u!").removesuffix("/driveItem")
            url = base64.urlsafe_b64decode(share + "=" * (-len(share) % 4)).decode()
            found = {
                "id": f"file-in-{url.rsplit('/', 2)[-2]}",
                "parentReference": {"driveId": "b!d"},
            }
            return httpx.Response(200, json=found)
        item_id = path.removeprefix("/v1.0/drives/b!d/items/")
        if (parent := item_id.removeprefix("file-in-")) != item_id:
            drive = "b!x" if parent == "elsewhere" else "b!d"
            ref = {"driveId": drive, "id": "general" if drive == "b!x" else parent}
            body = {
                "id": item_id,
                "parentReference": ref | {"path": "/drives/b!d/root:/General"},
                "@microsoft.graph.downloadUrl": _DOWNLOAD,
            }
            return httpx.Response(200, json=body)
        if item_id in _TREE:
            up = _TREE[item_id]
            ref = {"driveId": "b!d"} | ({} if up is None else {"id": up})
            return httpx.Response(200, json={"id": item_id, "parentReference": ref})
        return httpx.Response(404, json={"error": {"code": "itemNotFound"}})

    async def token() -> str:
        return "graph-token"

    return SharePoint(GraphClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), token))


@pytest.mark.parametrize("parent", ["general", "q3", f"level-{MAX_FOLDER_DEPTH - 1}"])
async def test_download_url_takes_a_file_in_the_folder_or_a_subfolder(parent: str) -> None:
    """The folder's id among the file's parents, up to the depth limit, is inside."""
    url = await _sharepoint([]).download_url(f"{_LIBRARY}/{parent}/a.xlsx", folder=_FOLDER)
    assert url == _DOWNLOAD, f"a file under {parent} is the channel's"


@pytest.mark.parametrize(
    ("parent", "where"),
    [
        ("general-2", "a sibling sharing the folder's name as a prefix"),
        ("board", "another folder of the same library"),
        ("root", "the library's root"),
        ("elsewhere", "a parent with the folder's id in another drive"),
        (f"level-{MAX_FOLDER_DEPTH}", "a file deeper than the walk goes"),
    ],
)
async def test_download_url_refuses_a_file_whose_parents_miss_the_folder(
    parent: str, where: str
) -> None:
    """A path naming the channel's folder is not trusted: only the parents' ids count."""
    with pytest.raises(GraphUnavailable, match="outside the channel's folder") as refused:
        await _sharepoint([]).download_url(f"{_LIBRARY}/{parent}/a.xlsx", folder=_FOLDER)
    assert refused.value.status == 200, f"{where}: unreachable, not a missing permission"


async def test_a_file_directly_in_the_folder_costs_no_folder_read() -> None:
    """The share, then the item: its parent's id answers without reading any folder."""
    seen: list[str] = []
    await _sharepoint(seen).download_url(f"{_LIBRARY}/general/a.xlsx", folder=_FOLDER)
    assert seen[1:] == ["/v1.0/drives/b!d/items/file-in-general"], "two calls, no folder read"


_ITEM = DriveItem.model_validate(
    {"id": "deck", "name": "deck.pptx", "eTag": '"{A},1"', "parentReference": {"driveId": "b!d"}}
)


def _writer(status: int, seen: list[httpx.Request]) -> SharePoint:
    """Graph answering every write with `status`; a 200 returns the new version."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "refused"}})
        return httpx.Response(200, json={"id": "deck", "name": "deck.pptx", "eTag": '"{A},2"'})

    async def token() -> str:
        return "graph-token"

    return SharePoint(GraphClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), token))


async def test_shared_item_carries_the_etag_a_replace_needs() -> None:
    """The item read by drive and id brings its eTag, the version a later write checks."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1.0/shares/"):
            return httpx.Response(200, json={"id": "deck", "parentReference": {"driveId": "b!d"}})
        ref = {"driveId": "b!d", "id": "general"}
        return httpx.Response(200, json={"id": "deck", "eTag": '"{A},1"', "parentReference": ref})

    async def token() -> str:
        return "graph-token"

    graph = GraphClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), token)
    item = await SharePoint(graph).shared_item(f"{_LIBRARY}/general/deck.pptx", folder=_FOLDER)
    assert item.e_tag == '"{A},1"', "the item's eTag is read, not the share's view of it"


async def test_replace_puts_the_bytes_over_the_item_only_at_its_etag() -> None:
    """One PUT to the item's content, with `If-Match`; the reply is the new version."""
    seen: list[httpx.Request] = []
    saved = await _writer(200, seen).replace(_ITEM, b"new deck", etag='"{A},1"')
    assert [(r.method, r.url.path) for r in seen] == [
        ("PUT", "/v1.0/drives/b!d/items/deck/content")
    ], "one write, to the item itself rather than by name"
    assert seen[0].headers["If-Match"] == '"{A},1"', "the write is conditional on the eTag"
    assert seen[0].headers["Authorization"] == "Bearer graph-token", "the token still goes"
    assert seen[0].content == b"new deck", "the staged bytes are the body"
    assert saved.e_tag == '"{A},2"', "the new version's eTag comes back"
    assert saved.parent_reference is not None and saved.parent_reference.drive_id == "b!d", (
        "a reply without its drive keeps the item's"
    )


@pytest.mark.parametrize(
    ("status", "why"),
    [(412, "changed since it was read"), (423, "locked"), (403, "refused")],
)
async def test_replace_raises_graphs_refusal_with_its_status_and_never_retries(
    status: int, why: str
) -> None:
    """The caller tells a changed, locked or refused file apart by the status."""
    seen: list[httpx.Request] = []
    with pytest.raises(GraphUnavailable) as refused:
        await _writer(status, seen).replace(_ITEM, b"new deck", etag='"{A},1"')
    assert refused.value.status == status, f"a file {why} keeps Graph's status"
    assert len(seen) == 1, "a write is never retried"
