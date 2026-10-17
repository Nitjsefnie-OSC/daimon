"""Which Microsoft Graph v1.0 paths the Microsoft tools may call.

A path is relative to the v1.0 root and must match one of a few shapes under
OneDrive, SharePoint sites, mail, calendar and OneNote. Anything else, any
absolute URL, dot segment, encoded separator or unlisted query key is refused
before a request is made. Mail, calendar and OneNote are personal: callers
show them only in the person's 1:1 chat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl

from fastmcp.exceptions import ToolError

WriteKind = Literal["upload", "workbook", "onenote_create", "onenote_update"]

_FILES = frozenset({"drive", "drives", "sites"})
_PERSONAL = frozenset(
    {"messages", "mailfolders", "events", "calendarview", "calendar", "calendars", "onenote"}
)
_QUERY_KEYS = frozenset(
    {
        "$select",
        "$top",
        "$skip",
        "$filter",
        "$orderby",
        "$search",
        "$count",
        "$expand",
        "$skiptoken",
        "startdatetime",
        "enddatetime",
        "search",
    }
)
_ENCODED = ("%2e", "%2f", "%5c", "%00", "%3f", "%23", "%25")
_DRIVE = r"/(?:me/drive|drives/[^/:]+|sites/[^/:]+/drive)"
_ITEM = _DRIVE + r"/(?:root|items/[^/:]+)(?::/[^:]+:)?"
_WRITES: tuple[tuple[frozenset[str], re.Pattern[str], WriteKind], ...] = (
    (frozenset({"PUT"}), re.compile(_ITEM + r"/content", re.I), "upload"),
    (frozenset({"POST", "PATCH"}), re.compile(_ITEM + r"/workbook/.+", re.I), "workbook"),
    (
        frozenset({"POST"}),
        re.compile(r"/me/onenote/(?:sections/[^/:]+/)?pages", re.I),
        "onenote_create",
    ),
    (frozenset({"PATCH"}), re.compile(r"/me/onenote/pages/[^/:]+/content", re.I), "onenote_update"),
)


@dataclass(frozen=True, slots=True)
class GraphPath:
    path: str
    query: tuple[tuple[str, str], ...]
    personal: bool


def _refuse(why: str) -> ToolError:
    return ToolError(
        f"Refused Graph path: {why}. Use a path relative to the v1.0 root under /me/drive, "
        "/drives, /sites, /me/messages, /me/mailFolders, /me/events, /me/calendarView or "
        "/me/onenote."
    )


def parse_path(raw: str) -> GraphPath:
    """Split and check a path any tool may read; raise `ToolError` when refused."""
    if "://" in raw or raw.startswith("//") or not raw.startswith("/"):
        raise _refuse("only a path such as /me/drive/root/children is accepted, not a URL")
    if any(ch in raw for ch in "\\#") or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise _refuse("it contains a backslash, fragment or control character")
    if any(code in raw.lower() for code in _ENCODED):
        raise _refuse("it contains an encoded dot, slash, backslash, query or percent sign")
    path, _, query = raw.partition("?")
    if path.lower().startswith("/v1.0/"):
        path = path[len("/v1.0") :]
    segments = path.split("/")[1:]
    if any(segment.strip(":") in ("", ".", "..") for segment in segments):
        raise _refuse("it has an empty or dot segment")
    pairs = tuple(parse_qsl(query, keep_blank_values=True))
    if any(key.lower() not in _QUERY_KEYS for key, _ in pairs):
        raise _refuse("it has a query parameter outside $select, $top, $filter and the like")
    head = [segment.lower() for segment in segments[:2]]
    if head[0] in ("drives", "sites"):
        return GraphPath(path=path, query=pairs, personal=False)
    if head[0] == "me" and len(head) == 2 and head[1] in _FILES:
        return GraphPath(path=path, query=pairs, personal=False)
    if head[0] == "me" and len(head) == 2 and head[1] in _PERSONAL:
        return GraphPath(path=path, query=pairs, personal=True)
    raise _refuse("it is outside the files, mail, calendar and notes this tool reaches")


def check_write(method: str, raw: str) -> tuple[GraphPath, WriteKind]:
    """A path `microsoft_write` may change with `method`; raise `ToolError` when refused."""
    target = parse_path(raw)
    if target.query:
        raise _refuse("a write takes no query parameters")
    for methods, pattern, kind in _WRITES:
        if method in methods and pattern.fullmatch(target.path):
            return target, kind
    raise ToolError(
        f"Refused: {method} {target.path} is not a write this tool makes. It uploads or "
        "replaces a file (PUT .../content), edits an Excel workbook under a drive item "
        "(POST or PATCH .../workbook/...), creates a OneNote page (POST "
        "/me/onenote/sections/{id}/pages) or updates one (PATCH /me/onenote/pages/{id}/content)."
    )
