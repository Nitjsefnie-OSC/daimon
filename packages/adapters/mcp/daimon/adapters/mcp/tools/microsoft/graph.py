"""microsoft_search, microsoft_get, microsoft_write and microsoft_send.

Every call runs as the Teams person with their own delegated token, over the
path allowlist in `_paths`. What Graph returns is other people's text and is
marked untrusted. Mail, calendar and OneNote work only in the person's 1:1
chat with Daimon, checked against the verified turn origin.
"""

from __future__ import annotations

import json
import re
from typing import Literal, cast

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._file_handles import staged_uploads
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.adapters.mcp.tools._untrusted import UntrustedResult
from daimon.adapters.mcp.tools.microsoft._access import GRAPH_ROOT
from daimon.adapters.mcp.tools.microsoft._paths import check_write, parse_path
from daimon.adapters.mcp.tools.microsoft._person import require_one_to_one, require_person
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, JsonValue

SearchKind = Literal["driveItem", "listItem", "message", "event"]
_PERSONAL_KINDS = frozenset({"message", "event"})
_MAX_CHARS = 60_000
_HIT_KEYS = (
    "id",
    "name",
    "subject",
    "webUrl",
    "webLink",
    "lastModifiedDateTime",
    "receivedDateTime",
    "from",
    "start",
    "end",
    "organizer",
    "size",
    "bodyPreview",
)
_ADDRESS = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+")
_MAX_RECIPIENTS = 50


class GraphResult(UntrustedResult):
    status: int
    data: JsonValue = None
    text: str | None = None
    next_path: str | None = None
    download_url: str | None = None
    truncated: bool = False


class SearchHit(BaseModel):
    kind: SearchKind
    path: str | None
    summary: str | None
    resource: dict[str, JsonValue]


class SearchResult(UntrustedResult):
    hits: list[SearchHit]


class SendResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["sent", "created"]
    message: str
    web_link: str | None = None


def _refused(response: httpx.Response) -> ToolError:
    try:
        error = cast(dict[str, JsonValue], response.json()).get("error")
    except ValueError:
        error = None
    code = message = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")[:300]
    return ToolError(f"Microsoft Graph refused ({response.status_code} {code}): {message}".strip())


def _next_path(data: dict[str, JsonValue]) -> str | None:
    link = data.pop("@odata.nextLink", None)
    if isinstance(link, str) and link.startswith(GRAPH_ROOT + "/"):
        return link[len(GRAPH_ROOT) :]
    return None


def _result(response: httpx.Response) -> GraphResult:
    """A bounded result: JSON trimmed by dropping list items, text cut, never bytes."""
    status = response.status_code
    if response.is_redirect:
        return GraphResult(status=status, download_url=response.headers.get("location"))
    if not response.content:
        return GraphResult(status=status)
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind == "application/json":
        data = cast(JsonValue, response.json())
        next_path = _next_path(data) if isinstance(data, dict) else None
        text = json.dumps(data)
        if len(text) <= _MAX_CHARS:
            return GraphResult(status=status, data=data, next_path=next_path)
        if isinstance(data, dict) and isinstance(data.get("value"), list):
            kept: list[JsonValue] = []
            budget = _MAX_CHARS - len(text) + len(json.dumps(data["value"]))
            for item in cast(list[JsonValue], data["value"]):
                budget -= len(json.dumps(item)) + 2
                if budget < 0:
                    break
                kept.append(item)
            if kept:
                return GraphResult(
                    status=status, data={**data, "value": kept}, next_path=next_path, truncated=True
                )
        return GraphResult(status=status, text=text[:_MAX_CHARS], truncated=True)
    if kind.startswith("text/") or kind in ("application/xhtml+xml", "application/xml"):
        text = response.text
        return GraphResult(status=status, text=text[:_MAX_CHARS], truncated=len(text) > _MAX_CHARS)
    return GraphResult(
        status=status,
        text=(
            "Binary content is not returned. GET the item without /content: its "
            "@microsoft.graph.downloadUrl downloads it."
        ),
    )


def _hit_path(kind: SearchKind, resource: dict[str, JsonValue]) -> str | None:
    item_id = resource.get("id")
    parent = resource.get("parentReference")
    parent = parent if isinstance(parent, dict) else {}
    ids = resource.get("sharepointIds")
    ids = ids if isinstance(ids, dict) else {}
    if kind == "driveItem" and item_id and parent.get("driveId"):
        return f"/drives/{parent['driveId']}/items/{item_id}"
    if kind == "listItem" and parent.get("siteId") and ids.get("listId") and ids.get("listItemId"):
        return f"/sites/{parent['siteId']}/lists/{ids['listId']}/items/{ids['listItemId']}"
    if kind == "message" and item_id:
        return f"/me/messages/{item_id}"
    if kind == "event" and item_id:
        return f"/me/events/{item_id}"
    return None


async def microsoft_search_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    query: str,
    kinds: list[SearchKind],
    size: int,
    origin_context_id: str,
) -> SearchResult:
    graph, user_id = require_person(runtime, auth)
    if not query.strip() or not kinds:
        raise ToolError("Pass a query and at least one kind.")
    if _PERSONAL_KINDS & set(kinds):
        await require_one_to_one(
            runtime, auth, origin_context_id, what="Searching mail or calendar"
        )
    hits: list[SearchHit] = []
    for kind in dict.fromkeys(kinds):
        body = {
            "requests": [
                {
                    "entityTypes": [kind],
                    "query": {"queryString": query},
                    "from": 0,
                    "size": max(1, min(size, 25)),
                }
            ]
        }
        response = await graph.request(auth.tenant_id, user_id, "POST", "/search/query", json=body)
        if response.is_error:
            raise _refused(response)
        for answer in cast(list[dict[str, JsonValue]], response.json().get("value") or []):
            for container in cast(list[dict[str, JsonValue]], answer.get("hitsContainers") or []):
                for hit in cast(list[dict[str, JsonValue]], container.get("hits") or []):
                    resource = hit.get("resource")
                    resource = cast(
                        dict[str, JsonValue], resource if isinstance(resource, dict) else {}
                    )
                    summary = hit.get("summary")
                    hits.append(
                        SearchHit(
                            kind=kind,
                            path=_hit_path(kind, resource),
                            summary=summary if isinstance(summary, str) else None,
                            resource={k: resource[k] for k in _HIT_KEYS if k in resource},
                        )
                    )
    return SearchResult(hits=hits)


async def microsoft_get_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, path: str, origin_context_id: str
) -> GraphResult:
    graph, user_id = require_person(runtime, auth)
    target = parse_path(path)
    if target.personal:
        await require_one_to_one(
            runtime, auth, origin_context_id, what="Mail, calendar and OneNote"
        )
    response = await graph.request(
        auth.tenant_id, user_id, "GET", target.path, params=dict(target.query) or None
    )
    if response.is_error:
        raise _refused(response)
    return _result(response)


async def microsoft_write_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    method: Literal["POST", "PATCH", "PUT"],
    path: str,
    body: JsonValue,
    file_handle: str | None,
    origin_context_id: str,
) -> GraphResult:
    graph, user_id = require_person(runtime, auth)
    target, kind = check_write(method, path)
    if target.personal:
        await require_one_to_one(runtime, auth, origin_context_id, what="OneNote")
    if (kind == "upload") != (file_handle is not None):
        raise ToolError("A file upload (PUT .../content) takes file_handle; nothing else does.")
    content: bytes | None = None
    headers: dict[str, str] = {}
    payload: JsonValue = None
    if kind == "upload":
        assert file_handle is not None
        if body is not None:
            raise ToolError("A file upload takes file_handle and no body.")
        ((upload, data),) = await staged_uploads(
            [file_handle], session_factory=runtime.session_factory, tenant_id=auth.tenant_id
        )
        content, headers = data, {"Content-Type": upload.content_type or "application/octet-stream"}
    elif kind == "onenote_create":
        if not isinstance(body, str) or not body.strip():
            raise ToolError("Creating a OneNote page takes the page's HTML as a string body.")
        content, headers = body.encode(), {"Content-Type": "text/html"}
    elif kind == "onenote_update":
        if not isinstance(body, list):
            raise ToolError("Updating a OneNote page takes a JSON list of patch commands as body.")
        payload = body
    else:
        if body is not None and not isinstance(body, dict):
            raise ToolError("A workbook call takes a JSON object body, or none.")
        payload = body
    response = await graph.request(
        auth.tenant_id,
        user_id,
        method,
        target.path,
        json=payload,
        content=content,
        headers=headers,
    )
    if response.is_error:
        raise _refused(response)
    return _result(response)


def _recipients(addresses: list[str], field: str) -> list[dict[str, object]]:
    for address in addresses:
        if not _ADDRESS.fullmatch(address.strip()):
            raise ToolError(f"{field} has {address!r}, which is not one email address.")
    return [{"emailAddress": {"address": a.strip()}} for a in addresses]


_GAP_TEXT = {
    "no_origin": "this call is not from the person's own chat turn",
    "no_live_session": "this chat has no live session for the person",
    "other_agents_session": "this chat's session runs another agent",
    "session_not_gated": (
        "this chat's session started before sending was set up; their next message updates it"
    ),
    "session_unreadable": "Daimon could not read this chat's session; try again in a moment",
    "source_context_unavailable": "this chat's session could not be checked",
}


async def microsoft_send_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    action: Literal["send_mail", "create_event"],
    to: list[str],
    subject: str,
    body: str,
    cc: list[str],
    start: str | None,
    end: str | None,
    time_zone: str,
    location: str | None,
    origin_context_id: str,
) -> SendResult:
    graph, user_id = require_person(runtime, auth)
    origin = await require_one_to_one(
        runtime, auth, origin_context_id, what="Sending mail or invites"
    )
    gap = await session_card_gap(runtime, auth, origin, tool_name="microsoft_send")
    if gap is not None:
        raise ToolError(
            "Nothing was sent: sending needs the person's Approve on a card, and "
            f"{_GAP_TEXT[gap]}. Do not retry this turn."
        )
    if not to or len(to) + len(cc) > _MAX_RECIPIENTS:
        raise ToolError(f"Pass between 1 and {_MAX_RECIPIENTS} recipients.")
    text = {"contentType": "Text", "content": body}
    if action == "send_mail":
        message: dict[str, object] = {
            "subject": subject,
            "body": text,
            "toRecipients": _recipients(to, "to"),
            "ccRecipients": _recipients(cc, "cc"),
        }
        payload: dict[str, object] = {"message": message, "saveToSentItems": True}
        response = await graph.request(
            auth.tenant_id, user_id, "POST", "/me/sendMail", json=payload
        )
        if response.is_error:
            raise _refused(response)
        return SendResult(status="sent", message="Sent from the person's mailbox.")
    if not start or not end:
        raise ToolError("An invite needs start and end, as ISO 8601 local times.")
    attendees: list[object] = [
        *({**r, "type": "required"} for r in _recipients(to, "to")),
        *({**r, "type": "optional"} for r in _recipients(cc, "cc")),
    ]
    event: dict[str, object] = {
        "subject": subject,
        "body": text,
        "start": {"dateTime": start, "timeZone": time_zone},
        "end": {"dateTime": end, "timeZone": time_zone},
        "attendees": attendees,
    }
    if location:
        event["location"] = {"displayName": location}
    response = await graph.request(auth.tenant_id, user_id, "POST", "/me/events", json=event)
    if response.is_error:
        raise _refused(response)
    link = cast(dict[str, JsonValue], response.json()).get("webLink")
    return SendResult(
        status="created",
        message="The event is on the person's calendar and the invites went out.",
        web_link=link if isinstance(link, str) else None,
    )
