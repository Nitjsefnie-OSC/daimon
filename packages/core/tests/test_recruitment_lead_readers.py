"""Independent reader families refuse reserved channels before content/row access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import httpx
import pytest
from daimon.core.config import RecruitmentSettings


def policy():
    return RecruitmentSettings(tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}})


def channel():
    value = MagicMock(spec=discord.Thread)
    value.id, value.parent_id, value.guild.id = 20, 2, 101
    return value


def interaction():
    value = MagicMock(spec=discord.Interaction)
    value.channel, value.guild_id = channel(), 101
    value.client = SimpleNamespace(
        runtime=SimpleNamespace(settings=SimpleNamespace(recruitment=policy()))
    )
    value.response.is_done.return_value = False
    value.response.send_message = AsyncMock()
    value.followup.send = AsyncMock()
    return value


@pytest.mark.parametrize(
    "path",
    [
        "channels/20/messages/30",
        "channels/20/messages",
        "channels/20/threads",
        "channels/2/messages",
    ],
)
async def test_lead_source_and_recovery_transport_refuse_before_message_http(path):
    from daimon.adapters.mcp.services.lead_intake import HttpIntakePorts

    requested = []

    def replay(request):
        requested.append(request.url.path)
        assert request.url.path in {"/api/v10/channels/20", "/api/v10/channels/2"}
        return httpx.Response(
            200,
            json={
                "id": request.url.path.rsplit("/", 1)[1],
                "guild_id": "101",
                "type": 11,
                "parent_id": "2",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        ports = HttpIntakePorts(
            http,
            gmail_token="fake",
            hubspot_token="fake",
            discord_token="fake",
            recruitment=policy(),
        )
        with pytest.raises(PermissionError, match="Source unavailable"):
            await ports.request("discord", "GET", path)
    assert len(requested) == 1


async def test_native_lead_panel_refuses_before_configuration_or_rows():
    from daimon.adapters.discord.lead_access import GatewayNativeLeadPermissions, discord_lead_reads

    origin = channel()
    store = SimpleNamespace(configuration=AsyncMock(side_effect=AssertionError("candidate rows")))
    with pytest.raises(PermissionError, match="Source unavailable"):
        await discord_lead_reads(
            sessionmaker=MagicMock(),
            store=store,
            resolver=MagicMock(),
            tenant_id=uuid4(),
            guild=origin.guild,
            user_id="7",
            origin=origin,
            recruitment=policy(),
        )
    guild = SimpleNamespace(id=101, fetch_channel=AsyncMock(return_value=origin))
    assert await GatewayNativeLeadPermissions(guild, MagicMock(), policy()).channel("20") is None
    from daimon.adapters.mcp.tools.lead_intake import McpNativeLeadPermissions

    member = SimpleNamespace(guild=SimpleNamespace(id=101))
    client = SimpleNamespace(fetch_channel=AsyncMock(return_value=origin))
    assert await McpNativeLeadPermissions(client, member, policy()).channel("20") is None
    store.configuration.assert_not_awaited()


async def test_lead_manual_source_refuses_candidate_thread_before_content():
    from daimon.adapters.mcp.services.lead_intake import HttpIntakePorts
    from daimon.core.lead_intake import LeadSource

    paths = []

    def replay(request):
        paths.append(request.url.path)
        assert "/messages" not in request.url.path
        return httpx.Response(
            200, json={"id": "20", "guild_id": "101", "parent_id": "2", "type": 11}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as client:
        ports = HttpIntakePorts(
            client,
            gmail_token="fake",
            hubspot_token="fake",
            discord_token="fake",
            recruitment=policy(),
        )
        with pytest.raises(PermissionError, match="Source unavailable"):
            await ports.source(
                SimpleNamespace(guild_id="101"),
                LeadSource(kind="manual", channel_id="20", message_id="30"),
                "7",
            )
    assert all("/messages" not in path for path in paths)
