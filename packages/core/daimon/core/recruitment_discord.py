"""Bounded Discord REST ports shared by trusted staffing coordinators.

No gateway consumer and no provider session. All permission facts are fetched
at use time, including parent overwrites and private-thread membership.
"""

from datetime import datetime

import httpx
from daimon.core.recruitment import RecruitmentConfig, SourceMessage, may_decide
from pydantic import BaseModel, Field


class Overwrite(BaseModel):
    id: str
    type: int
    allow: str
    deny: str


class ThreadMetadata(BaseModel):
    archived: bool = False
    locked: bool = False


class DiscordChannel(BaseModel):
    id: str
    guild_id: str | None = None
    parent_id: str | None = None
    name: str = ""
    type: int
    permission_overwrites: list[Overwrite] = Field(default_factory=lambda: list[Overwrite]())
    thread_metadata: ThreadMetadata = Field(default_factory=ThreadMetadata)


class DiscordUser(BaseModel):
    id: str
    bot: bool = False


class DiscordMember(BaseModel):
    user: DiscordUser
    roles: tuple[str, ...] = ()


class DiscordRole(BaseModel):
    id: str
    permissions: str


class DiscordGuild(BaseModel):
    id: str
    owner_id: str


class Attachment(BaseModel):
    filename: str
    url: str


class EmbedField(BaseModel):
    name: str
    value: str


class Embed(BaseModel):
    title: str = ""
    description: str = ""
    url: str = ""
    fields: list[EmbedField] = Field(default_factory=lambda: list[EmbedField]())


class DiscordMessage(BaseModel):
    id: str
    content: str = ""
    author: DiscordUser
    timestamp: datetime
    edited_timestamp: datetime | None = None
    embeds: list[Embed] = Field(default_factory=lambda: list[Embed]())
    attachments: list[Attachment] = Field(default_factory=lambda: list[Attachment]())
    components: list[dict[str, object]] = Field(default_factory=lambda: list[dict[str, object]]())

    def source(self, guild: str, thread: str) -> SourceMessage | None:
        if self.author.bot:
            return None
        content = "\n".join(
            [
                self.content,
                *[
                    "\n".join(
                        [e.title, e.description, e.url, *[f"{f.name}: {f.value}" for f in e.fields]]
                    )
                    for e in self.embeds
                ],
                *[f"Attachment: {a.filename} {a.url}" for a in self.attachments],
            ]
        )
        return SourceMessage(
            id=self.id,
            content=content,
            observed_at=self.edited_timestamp or self.timestamp,
            url=f"https://discord.com/channels/{guild}/{thread}/{self.id}",
        )


class DiscordResourceMissing(PermissionError):
    pass


class RecruitmentDiscord:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self.client, self.token = client, token

    async def request(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        response = await self.client.get(
            "https://discord.com/api/v10/" + path,
            headers={"Authorization": f"Bot {self.token}"},
            params=params,
            timeout=30,
            follow_redirects=False,
        )
        if response.status_code == 404:
            raise DiscordResourceMissing("resource_missing")
        if response.status_code == 403:
            raise PermissionError("thread_unavailable")
        response.raise_for_status()
        return response

    async def channel(self, thread: str) -> DiscordChannel:
        channel = DiscordChannel.model_validate((await self.request(f"channels/{thread}")).json())
        if channel.type not in {10, 11, 12} or not channel.parent_id:
            raise PermissionError("not_a_thread")
        return channel

    async def check_access(
        self, config: RecruitmentConfig, thread: str, user: str, *, recruiter: bool = True
    ) -> None:
        channel = await self.channel(thread)
        if (
            channel.guild_id != config.guild_id
            or channel.thread_metadata.archived
            or channel.thread_metadata.locked
        ):
            raise PermissionError("thread_unavailable")
        parent = DiscordChannel.model_validate(
            (await self.request(f"channels/{channel.parent_id}")).json()
        )
        guild = DiscordGuild.model_validate(
            (await self.request(f"guilds/{config.guild_id}")).json()
        )
        member = DiscordMember.model_validate(
            (await self.request(f"guilds/{config.guild_id}/members/{user}")).json()
        )
        roles = [
            DiscordRole.model_validate(r)
            for r in (await self.request(f"guilds/{config.guild_id}/roles")).json()
        ]
        if parent.guild_id != config.guild_id or member.user.id != user:
            raise PermissionError("guild_mismatch")
        permissions = 0
        for role in roles:
            if role.id == config.guild_id or role.id in member.roles:
                permissions |= int(role.permissions)
        admin = bool(permissions & 8) or guild.owner_id == user
        if not admin:
            for overwrite in parent.permission_overwrites:
                if overwrite.type == 0 and overwrite.id == config.guild_id:
                    permissions = permissions & ~int(overwrite.deny) | int(overwrite.allow)
            allow, deny = 0, 0
            for overwrite in parent.permission_overwrites:
                if overwrite.type == 0 and overwrite.id in member.roles:
                    allow |= int(overwrite.allow)
                    deny |= int(overwrite.deny)
            permissions = permissions & ~deny | allow
            for overwrite in parent.permission_overwrites:
                if overwrite.type == 1 and overwrite.id == user:
                    permissions = permissions & ~int(overwrite.deny) | int(overwrite.allow)
        required = (1 << 10) | (1 << 16)
        if not recruiter:
            required |= 1 << 38  # The bot must also be able to display status in this thread.
        accessible = admin or permissions & required == required
        if channel.type == 12 and not admin and not permissions & (1 << 34):
            await self.request(f"channels/{thread}/thread-members/{user}")
        if (
            not accessible
            or recruiter
            and not may_decide(config, user, member.roles, can_access=True)
        ):
            raise PermissionError("recruiter_access_required")

    async def history(
        self,
        config: RecruitmentConfig,
        thread: str,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> tuple[list[SourceMessage], str | None, str]:
        params = {"limit": "100"}
        if before:
            params["before"] = before
        if after and after != "0":
            params["after"] = after
        raw = [
            DiscordMessage.model_validate(m)
            for m in (await self.request(f"channels/{thread}/messages", params=params)).json()
        ]
        messages = [source for m in raw if (source := m.source(config.guild_id, thread))]
        return (
            messages,
            min((m.id for m in raw), key=int) if len(raw) == 100 else None,
            max((m.id for m in raw), key=int, default=after or "0"),
        )

    async def message(
        self, config: RecruitmentConfig, thread: str, id: str
    ) -> SourceMessage | None:
        raw = DiscordMessage.model_validate(
            (await self.request(f"channels/{thread}/messages/{id}")).json()
        )
        return raw.source(config.guild_id, thread)

    async def bot_id(self) -> str:
        return DiscordUser.model_validate((await self.request("users/@me")).json()).id

    async def active(self, config: RecruitmentConfig) -> list[DiscordChannel]:
        response = await self.request(f"guilds/{config.guild_id}/threads/active")
        return [DiscordChannel.model_validate(t) for t in response.json()["threads"]]
