"""Mirror tools and the `ask` hand-off: admin-only, Discord-only, rule refusals, audit."""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import daimon.adapters.mcp.tools.mirrors as tools
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.hosted_artifacts import HostedChartDelivery
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agent_chat import _ask_impl  # pyright: ignore[reportPrivateUsage]
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import MirrorsSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import mirrors as mirrors_store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.security_audit import list_events
from daimon.testing import ma_agent, ma_model_config
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_create = tools._create_mirror_impl  # pyright: ignore[reportPrivateUsage]
_list = tools._list_mirrors_impl  # pyright: ignore[reportPrivateUsage]
_remove = tools._remove_mirror_impl  # pyright: ignore[reportPrivateUsage]
_NOW = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)


def _runtime(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, *, enabled: bool = True
) -> McpRuntime:
    agent = ma_agent(
        id="ag_helper",
        name="helper",
        model=ma_model_config("claude-sonnet-4-6", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: "helper"},
    )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent.model_dump(mode="json")]))
    client: AsyncAnthropic = build_fake_anthropic(router.dispatch)
    settings = MagicMock()
    settings.mirrors = MirrorsSettings(enabled=enabled)
    settings.artifacts = None
    return McpRuntime(
        session_factory=sm,
        client=client,
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


async def _auth(
    session: AsyncSession, tenant: TenantRow, *, is_admin: bool = True, platform: str = "discord"
) -> AuthIdentity:
    return AuthIdentity(
        account_id=(await make_account(session, tenant=tenant)).id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform=platform,
        platform_user_id="9001",
        is_admin=is_admin,
    )


async def _create_in(runtime: McpRuntime, auth: AuthIdentity, dest: str, **kw: object):
    return await _create(
        runtime,
        auth,
        name="qa",
        agent_name=str(kw.get("agent", "helper")),
        destination_id=dest,
        destination_kind="channel",
        consent_note=kw.get("consent"),  # type: ignore[arg-type]
        max_answer_chars=None,
    )


async def test_create_refuses_members_disabled_deployments_and_other_platforms(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    runtime, admin = _runtime(sessionmaker, tenant.id), await _auth(db_session, tenant)
    member = await _auth(db_session, tenant, is_admin=False)

    with pytest.raises(ToolError):
        await _create_in(runtime, member, "300")
    with pytest.raises(ToolError, match="switched off"):
        await _create_in(_runtime(sessionmaker, tenant.id, enabled=False), admin, "300")
    for platform in ("slack", "teams"):
        with pytest.raises(ToolError, match="only into Discord"):
            await _create_in(runtime, await _auth(db_session, tenant, platform=platform), "C0123")
    with pytest.raises(ToolError, match="no agent named"):
        await _create_in(runtime, admin, "300", agent="ghost")
    with pytest.raises(ToolError):
        await _create_in(runtime, admin, "not-a-snowflake")
    assert await mirrors_store.list_for_tenant(db_session, tenant.id) == [], "nothing saved"


async def test_create_refuses_a_home_kept_to_other_agents(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(channel_rules={"100": ChannelRule(readers="own", writers="own")}),
    )

    admin = await _auth(db_session, tenant)

    with pytest.raises(ToolError, match="Refused"):
        await _create_in(_runtime(sessionmaker, tenant.id), admin, "100")


async def test_create_records_attested_consent_in_the_audit_trail_and_lists_it(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    runtime, auth = _runtime(sessionmaker, tenant.id), await _auth(db_session, tenant)

    created = await _create_in(runtime, auth, "300", consent="team agreed on 1 Oct")
    listed = await _list(runtime, auth)

    assert created.consent is not None and "team agreed" in created.consent, created
    assert [m.name for m in listed] == ["qa"], listed
    events = await list_events(db_session, tenant_id=tenant.id)
    assert [(e.tool_name, e.operation) for e in events] == [("create_mirror", "attest_consent")]
    assert "team agreed on 1 Oct" in events[0].reason, "the attestation is audited"
    assert await _remove(runtime, auth, "qa"), "remove reports success"
    assert await _list(runtime, auth) == [], "removed"


async def _ask(runtime: McpRuntime, auth: AuthIdentity):
    idle = MagicMock(status="idle")
    events = MagicMock(
        items=[MagicMock(type="agent.message", content=[{"type": "text", "text": "Answer"}])],
        next_page=None,
    )
    started = {"handle": "sesn_1", "turn_event_id": "e0", "turn_started_at": _NOW.isoformat()}
    module = "daimon.adapters.mcp.tools.agent_chat"
    with (
        patch(f"{module}._start_turn_impl", new=AsyncMock(return_value=started)),
        patch(f"{module}._get_session_impl", new=AsyncMock(return_value=idle)),
        patch(f"{module}._list_events_impl", new=AsyncMock(return_value=events)),
        patch(
            f"{module}.deliver_hosted_charts",
            new=AsyncMock(return_value=HostedChartDelivery(message="Answer")),
        ),
    ):
        return await _ask_impl(
            runtime, auth, "Question", sleep=AsyncMock(), clock=lambda: 0.0, now=lambda: _NOW
        )


@pytest.mark.parametrize("enabled", [True, False])
async def test_ask_queues_the_answer_for_a_mirror_and_says_where(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession, enabled: bool
) -> None:
    tenant = await make_tenant(db_session)
    runtime = _runtime(sessionmaker, tenant.id)
    await _create_in(runtime, await _auth(db_session, tenant), "300", consent="agreed")
    runtime.settings.mirrors = MirrorsSettings(enabled=enabled)
    caller = AuthIdentity(
        account_id=(await make_account(db_session, tenant=tenant)).id,
        tenant_id=tenant.id,
        role=Role.USER,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_helper"),
    )

    result = await _ask(runtime, caller)

    claimed = await mirrors_store.claim_mirror_deliveries(
        db_session,
        platform="discord",
        owner="t",
        now=_NOW,
        lease=dt.timedelta(minutes=1),
        max_pending_age=dt.timedelta(minutes=30),
    )
    if enabled:
        assert result.shared_to == ("discord channel 300",), result
        assert claimed[0].payload is not None and claimed[0].payload.answer == "Answer"
    else:
        assert (result.shared_to, claimed) == ((), []), "a disabled deployment queues nothing"
