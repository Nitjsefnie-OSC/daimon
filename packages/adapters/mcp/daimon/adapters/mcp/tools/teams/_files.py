"""Files send_message posts on Teams: saved to a channel's Files, offered in a 1:1 chat.

In a channel each file is saved to the channel's Files folder (`filesFolder`)
through Graph under `Files.ReadWrite.All`, as the adapter saves session
outputs, and the message links to it. A bot can put a file in a 1:1 chat only
through a FileConsentCard: the person accepts and the adapter uploads the
staged bytes to their OneDrive (`daimon.core.teams_file_offers`). A group chat
takes neither.

The read tools link a message's shared files (`file_links`): Graph's download
URL, only for a file inside the channel's Files folder. The permission reads
past SharePoint's per-item permissions, so a file posted from another site,
library or folder is listed by name only. The URL is pre-authorised for about
an hour, as a Slack read's proxy link lasts the turn grant, and only messages
the read already returns get one, so it shows nothing the caller could not read.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable

import httpx
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._directory import (
    TeamsChannelRef,
    graph_for,
    locate_channel,
    split_thread,
)
from daimon.core.media.filenames import sanitize_title
from daimon.core.stores.domain import FileUploadRow
from daimon.core.teams_file_offers import UploadOffer, consent_attachment
from daimon.core.teams_graph import FILE_ATTACHMENT_TYPES, GraphMessage, GraphUnavailable
from daimon.core.teams_sharepoint import SharePoint, file_link
from fastmcp.exceptions import ToolError

_log = structlog.get_logger(__name__)

MAX_FILES = 10
# Links resolved per read: each costs a Graph call, the rest stay names only.
MAX_LINKS = 20
# A channel's conversation id; a group chat's ends in `@thread.v2`.
CHANNEL = re.compile(r"19:[^;]+@thread\.(?:tacv2|skype)")
_NO_FILES = (
    "daimon cannot save files in that channel: Graph refused it the channel's Files folder. "
    "A Microsoft 365 admin must grant daimon's app the Files.ReadWrite.All application "
    "permission. Nothing was posted"
)

Staged = list[tuple[FileUploadRow, bytes]]


async def post_files(
    runtime: McpRuntime,
    auth: AuthIdentity,
    client: TeamsBotClient,
    conversation_id: str,
    content: str,
    staged: Staged,
) -> str:
    """Post `content` with the files; returns the first activity id."""
    if conversation_id.startswith("a:"):
        return await _offer(auth, client, conversation_id, content, staged)
    if CHANNEL.fullmatch(split_thread(conversation_id)[0]):
        return await _save_in_channel(runtime, auth, client, conversation_id, content, staged)
    raise ToolError("Teams takes files in a channel or a 1:1 chat with daimon, not a group chat")


async def _save_in_channel(
    runtime: McpRuntime,
    auth: AuthIdentity,
    client: TeamsBotClient,
    conversation_id: str,
    content: str,
    staged: Staged,
) -> str:
    ref = await locate_channel(runtime, auth, client, split_thread(conversation_id)[0])
    sharepoint = SharePoint(graph_for(client))
    links: list[str] = []
    try:
        folder = await sharepoint.channel_folder(ref.group_id, ref.channel_id)
        for upload, data in staged:
            item = await sharepoint.upload(folder, upload.display_filename, data)
            links.append(
                file_link(sanitize_title(item.name or upload.display_filename), item.web_url)
            )
    except GraphUnavailable as err:
        saved = f" ({len(links)} file(s) were saved to its Files first)" if links else ""
        raise ToolError(f"{_NO_FILES}{saved}.") from err
    try:
        return await client.send(conversation_id, "\n\n".join(filter(None, [content, *links])))
    except (httpx.HTTPError, ValueError) as err:
        raise ToolError(
            f"the {len(links)} file(s) were saved to the channel's Files, but posting the "
            f"message failed ({type(err).__name__}); post it again without file_handles, "
            "with these links:\n" + "\n".join(links)
        ) from err


async def _offer(
    auth: AuthIdentity, client: TeamsBotClient, chat: str, content: str, staged: Staged
) -> str:
    user = str(uuid.UUID(auth.platform_user_id or ""))
    ids = [await client.send(chat, content)] if content.strip() else []
    for upload, data in staged:
        token = client.file_offer_token(UploadOffer(upload.id, user, chat))
        card = consent_attachment(upload.display_filename, len(data), token)
        try:
            ids.append(await client.send_activity(chat, {"type": "message", "attachments": [card]}))
        except (httpx.HTTPError, ValueError) as err:
            raise ToolError(
                f"offering {upload.display_filename!r} failed after {len(ids)} message(s) "
                f"({type(err).__name__})"
            ) from err
    return ids[0]


async def file_links(
    client: TeamsBotClient,
    ref: TeamsChannelRef,
    messages: Iterable[GraphMessage],
) -> dict[str, str]:
    """Download URLs of the messages' shared files inside the channel's folder, by content URL."""
    wanted = list(
        dict.fromkeys(
            a.content_url
            for m in messages
            for a in m.attachments
            if a.content_type in FILE_ATTACHMENT_TYPES and a.content_url
        )
    )[:MAX_LINKS]
    if not wanted:
        return {}
    sharepoint = SharePoint(graph_for(client))
    try:
        folder = await sharepoint.channel_folder(ref.group_id, ref.channel_id)
    except GraphUnavailable as err:
        _log.info("teams.file_link.unresolved", status=err.status, reason=err.reason)
        return {}
    links: dict[str, str] = {}
    for url in wanted:
        try:
            links[url] = await sharepoint.download_url(url, folder=folder)
        except GraphUnavailable as err:
            _log.info("teams.file_link.unresolved", status=err.status, reason=err.reason)
            if err.status in (401, 403):
                break  # The permission is not granted: no other file would resolve.
    return links
