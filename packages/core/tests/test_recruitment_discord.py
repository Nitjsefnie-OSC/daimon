"""Fresh Discord access checks with entirely synthetic REST responses."""

import httpx
import pytest
from daimon.core.recruitment import RecruitmentConfig
from daimon.core.recruitment_discord import RecruitmentDiscord


@pytest.mark.parametrize(
    "recruiter,private_member,revoked",
    [(True, True, False), (False, True, False), (True, False, False), (True, True, True)],
)
async def test_live_role_thread_membership_and_overwrites(
    recruiter: bool, private_member: bool, revoked: bool
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10/")
        if path == "channels/20":
            return httpx.Response(
                200, json={"id": "20", "guild_id": "101", "parent_id": "2", "type": 12}
            )
        if path == "channels/2":
            return httpx.Response(
                200,
                json={
                    "id": "2",
                    "guild_id": "101",
                    "type": 0,
                    "permission_overwrites": [
                        {"id": "7", "type": 1, "deny": str(1 << 10), "allow": "0"}
                    ]
                    if revoked
                    else [],
                },
            )
        if path == "guilds/101":
            return httpx.Response(200, json={"id": "101", "owner_id": "999"})
        if path == "guilds/101/members/7":
            return httpx.Response(
                200, json={"user": {"id": "7"}, "roles": ["42"] if recruiter else []}
            )
        if path == "guilds/101/roles":
            return httpx.Response(
                200,
                json=[
                    {"id": "101", "permissions": str((1 << 10) | (1 << 16))},
                    {"id": "42", "permissions": "0"},
                ],
            )
        if path == "channels/20/thread-members/7":
            return httpx.Response(200 if private_member else 404, json={})
        raise AssertionError(path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        discord = RecruitmentDiscord(http, "synthetic")
        config = RecruitmentConfig(guild_id="101", recruiter_role_ids=("42",))
        if recruiter and private_member and not revoked:
            await discord.check_access(config, "20", "7")
            with pytest.raises(PermissionError):
                await discord.check_access(config, "20", "7", recruiter=False)
        else:
            with pytest.raises(PermissionError):
                await discord.check_access(config, "20", "7")
