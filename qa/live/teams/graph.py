"""Bounded Microsoft Graph operations for one configured Teams QA channel."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html import escape, unescape
from typing import Any

import httpx

from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.tokens import TokenStore

BASE = "https://graph.microsoft.com/v1.0"
READ = ["ChannelMessage.Read.All", "offline_access"]
SEND = ["ChannelMessage.Send", "offline_access"]
TARGET = ["Team.ReadBasic.All", "Channel.ReadBasic.All", "User.Read", "offline_access"]
CHAT = ["Chat.ReadWrite", "User.Read", "offline_access"]


class GraphRequestError(RuntimeError):
    """A Graph response or read request that cannot be used."""


class AmbiguousPostError(GraphRequestError):
    """A POST may have reached Graph, so reconcile before trying again."""


def graph_time(value: object) -> datetime | None:
    """Parse a Graph timestamp without assuming its UTC spelling."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class GraphTeamsDriver:
    def __init__(
        self,
        config: TeamsQaConfig,
        tokens: TokenStore,
        http: httpx.AsyncClient,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.tokens = tokens
        self.http = http
        self.now = now
        self.sleep = sleep

    @property
    def channel(self) -> str:
        """Return the configured team and channel path."""
        return f"/teams/{self.config.team_id}/channels/{self.config.channel_id}"

    async def _request(
        self,
        persona: str,
        scopes: list[str],
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        token = self.tokens.acquire(persona, scopes)
        delay = 1.0
        for _attempt in range(5):
            if deadline is not None and self.now() >= deadline:
                raise GraphRequestError("Graph request deadline reached")
            try:
                response = await self.http.request(
                    method,
                    BASE + path,
                    headers={"Authorization": f"Bearer {token.value}"},
                    json=payload,
                    timeout=min(15, max(0.01, deadline - self.now()))
                    if deadline is not None
                    else 15,
                )
            except httpx.RequestError:
                if method == "POST":
                    raise AmbiguousPostError("Graph POST outcome unknown") from None
                raise GraphRequestError("Graph transport failed") from None
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if response.status_code == 429 and deadline is None:
                    deadline = self.now() + 30
                if deadline is None or self.now() >= deadline:
                    if method == "POST" and response.status_code >= 500:
                        raise AmbiguousPostError("Graph POST outcome unknown")
                    raise GraphRequestError("Graph request deadline reached")
                retry = response.headers.get("Retry-After", "")
                wait = float(retry) if retry.replace(".", "", 1).isdigit() else delay
                await self.sleep(min(wait, max(0, deadline - self.now())))
                delay = min(delay * 2, 8)
                continue
            if response.is_error:
                raise GraphRequestError(f"Graph returned HTTP {response.status_code}")
            return response.json()
        raise GraphRequestError("Graph request deadline reached")

    async def verify_target(self, persona: str) -> None:
        """Refuse a Graph target whose IDs or display names differ from config."""
        team = await self._request(persona, TARGET, "GET", f"/teams/{self.config.team_id}")
        channel = await self._request(persona, TARGET, "GET", self.channel)
        if (team.get("id"), team.get("displayName")) != (
            self.config.team_id,
            self.config.team_display_name,
        ):
            raise ValueError("team target mismatch")
        if (channel.get("id"), channel.get("displayName")) != (
            self.config.channel_id,
            self.config.channel_display_name,
        ):
            raise ValueError("channel target mismatch")
        if not self.config.bot_app_id:
            raise ValueError("bot app identity missing")

    async def find_by_correlation(
        self,
        persona: str,
        correlation_id: str,
        since: datetime | None = None,
        *,
        command_text: str | None = None,
    ) -> dict[str, Any] | None:
        """Search bounded recent messages for a marked or exact persona post."""
        path = self.channel + "/messages?$top=50"
        for _ in range(4):
            data = await self._request(persona, READ, "GET", path)
            for item in data.get("value", []):
                created_at = graph_time(item.get("createdDateTime"))
                if created_at is None:
                    continue
                if since and created_at < since - timedelta(seconds=10):
                    continue
                expected_oid = self.config.personas[persona].expected_oid
                if command_text is not None and expected_oid is None:
                    expected_oid = self.tokens.status(persona).get("oid")
                sender_oid = item.get("from", {}).get("user", {}).get("id")
                if expected_oid and sender_oid != expected_oid:
                    continue
                if command_text is not None and not expected_oid:
                    continue
                content = item.get("body", {}).get("content", "")
                if command_text is not None:
                    without_mention = re.sub(r"<at\b[^>]*>.*?</at>", "", content, flags=re.I | re.S)
                    plain = unescape(re.sub(r"<[^>]*>", "", without_mention)).strip()
                    if plain == command_text:
                        return item
                elif f"[qa:{correlation_id}]" in content:
                    return item
            next_link = data.get("@odata.nextLink")
            if not isinstance(next_link, str) or not next_link.startswith(BASE + self.channel):
                break
            path = next_link.removeprefix(BASE)
        return None

    async def post_mention(
        self, persona: str, text: str, correlation_id: str, *, command: bool = False
    ) -> dict[str, Any]:
        """Post a bot mention, reconciling an ambiguous first attempt."""
        bot = self.config.bot_display_name
        content = f'<at id="0">{escape(bot)}</at> {escape(text)}'
        if not command:
            content += f" [qa:{escape(correlation_id)}]"
        payload = {
            "body": {"contentType": "html", "content": content},
            "mentions": [
                {
                    "id": 0,
                    "mentionText": bot,
                    "mentioned": {
                        "application": {
                            "@odata.type": "#microsoft.graph.teamworkApplicationIdentity",
                            "id": self.config.bot_app_id,
                            "displayName": bot,
                            "applicationIdentityType": "bot",
                        }
                    },
                }
            ],
        }
        attempt_started = datetime.now().astimezone()
        for attempt in range(2):
            try:
                return await self._request(
                    persona, SEND, "POST", self.channel + "/messages", payload=payload
                )
            except AmbiguousPostError:
                if attempt:
                    raise
                await self.sleep(3)
                found = await self.find_by_correlation(
                    persona,
                    correlation_id,
                    attempt_started,
                    command_text=text if command else None,
                )
                if found:
                    return found
        raise RuntimeError("post outcome unavailable")

    async def read_replies(
        self, persona: str, message_id: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        """Read replies to one channel message."""
        if not message_id or "/" in message_id:
            raise ValueError("invalid message id")
        data = await self._request(
            persona,
            READ,
            "GET",
            self.channel + f"/messages/{message_id}/replies",
            deadline=deadline,
        )
        return list(data.get("value", []))

    async def wait_for_reply(
        self,
        persona: str,
        message_id: str,
        predicate: Callable[[dict[str, Any]], bool],
        deadline: float,
    ) -> dict[str, Any] | None:
        """Poll replies with backoff until a match or the hard deadline."""
        delay = 1.0
        while self.now() < deadline:
            for reply in await self.read_replies(persona, message_id, deadline):
                if predicate(reply):
                    return reply
            await self.sleep(min(delay, max(0, deadline - self.now())))
            delay = min(delay * 2, 8)
        return None

    async def chat_messages_with_bot(
        self, persona: str, since: datetime, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        """Find the sole bot chat and return its messages in the time window."""
        data = await self._request(
            persona, CHAT, "GET", "/me/chats?$expand=members", deadline=deadline
        )
        candidates = [chat for chat in data.get("value", []) if chat.get("chatType") == "oneOnOne"]
        if len(candidates) > 5:
            raise ValueError("too many one-on-one chat candidates")
        matches: list[list[dict[str, Any]]] = []
        for chat in candidates:
            chat_id = chat.get("id")
            if not isinstance(chat_id, str) or "/" in chat_id:
                continue
            response = await self._request(
                persona, CHAT, "GET", f"/chats/{chat_id}/messages", deadline=deadline
            )
            messages = list(response.get("value", []))
            member_match = any(
                member.get("userId") == self.config.bot_app_id
                or member.get("displayName") == self.config.bot_display_name
                for member in chat.get("members", [])
            )
            sender_match = any(
                message.get("from", {}).get("application", {}).get("id") == self.config.bot_app_id
                for message in messages
            )
            if member_match or sender_match:
                matches.append(messages)
        if len(matches) != 1:
            raise ValueError("bot chat unavailable or ambiguous")
        start = since - timedelta(seconds=10)
        return [
            message
            for message in matches[0]
            if (created := graph_time(message.get("createdDateTime"))) is not None
            and created >= start
        ]
