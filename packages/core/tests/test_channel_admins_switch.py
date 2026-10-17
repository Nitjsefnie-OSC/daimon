"""The deploy-time channel-admin switch keeps stored grants inert."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Surface, authorize, build_subject
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    StoredAdmin,
    administered_channel_ids,
    channel_admin_user_ids,
    is_channel_admin,
)
from daimon.core.channel_budget_notice import BudgetNotice, notice_recipients
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    Settings,
    channel_admins_enabled,
    prime_channel_admins_enabled,
    reset_channel_admins_enabled,
)
from daimon.core.github_panel import requester_manages_agent
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.channel_admins import (
    delete_channel_admins,
    get_channel_admins,
    list_administered_channel_ids,
    list_channel_admins,
    set_channel_admins,
)
from daimon.core.stores.domain import ChannelAdminsRow
from daimon.core.support_routing import support_recipient_tiers
from pydantic import PostgresDsn, SecretStr


@pytest.fixture
def admins_off(monkeypatch: pytest.MonkeyPatch):
    reset_channel_admins_enabled()
    monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "false")
    yield
    reset_channel_admins_enabled()


def test_grant_holders_lose_rights_but_server_admins_keep_them(admins_off: None) -> None:
    grant = ChannelAdminsRow(
        tenant_id=uuid.uuid4(),
        platform="discord",
        channel_id="channel",
        role_ids=("role",),
        user_ids=("user",),
        updated_by_account_id=None,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    holder = ChannelAdminCaller(platform_user_id="user", role_ids=frozenset({"role"}))
    admin = ChannelAdminCaller(platform_user_id="admin", is_server_admin=True)
    assert not is_channel_admin(holder, grant=grant)
    assert is_channel_admin(admin, grant=grant)
    assert administered_channel_ids(holder, [grant]) == frozenset()
    assert (
        build_subject(
            is_admin=False, platform_user_id="user", administered_channel_ids={"channel"}
        ).administered_channel_ids
        == frozenset()
    )
    assert build_subject(is_admin=True, platform_user_id="admin").is_admin


def test_former_holder_cannot_save_rules_or_use_channel_agent_in_dm_or_hub(
    admins_off: None,
) -> None:
    holder = build_subject(
        is_admin=False, platform_user_id="user", administered_channel_ids={"channel"}
    )
    admin = build_subject(is_admin=True, platform_user_id="admin")
    policy = TenantAccessPolicy.model_validate({"agent_channel_pins": {"helper": ["channel"]}})
    agent = AgentRef.of("helper")
    assert not authorize(
        policy,
        subject=holder,
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id="channel"),
    )
    assert not authorize(policy, subject=holder, action=Action.SET_AGENT_TOOL_RULE)
    assert authorize(policy, subject=admin, action=Action.SET_AGENT_TOOL_RULE)
    assert not authorize(
        policy, subject=holder, action=Action.RUN_AGENT, surface=Surface.DM, agent=agent
    )
    assert not authorize(
        policy, subject=holder, action=Action.RUN_AGENT, surface=Surface.HUB, agent=agent
    )
    assert authorize(
        policy, subject=admin, action=Action.RUN_AGENT, surface=Surface.HUB, agent=agent
    )


async def test_github_save_recheck_rejects_former_holder(
    admins_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core import github_panel

    stored = StoredAdmin(
        is_admin=False,
        platform="discord",
        platform_user_id="user",
        grants=(
            ChannelAdminsRow(
                tenant_id=uuid.uuid4(),
                platform="discord",
                channel_id="channel",
                role_ids=(),
                user_ids=("user",),
                updated_by_account_id=None,
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
    )
    read = AsyncMock(return_value=stored)
    monkeypatch.setattr(github_panel, "read_stored_admin", read)
    members = AsyncMock(return_value=frozenset({"user"}))
    allowed = await requester_manages_agent(
        AsyncMock(),
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        platform="discord",
        platform_user_id="user",
        agent_name="helper",
        ma_agent_id="agent-id",
        default=DeploymentDefault(),
        is_daimon_managed=False,
        members=members,
    )
    assert not allowed
    read.assert_awaited_once()
    members.assert_not_awaited()


async def test_granted_channels_use_server_admin_budget_dms_and_escalation(
    admins_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core import channel_budget_notice

    sessionmaker = MagicMock(return_value=AsyncMock())
    admins = AsyncMock(return_value=["server-admin"])
    monkeypatch.setattr(channel_budget_notice, "list_platform_user_ids", admins)
    tenant_id = uuid.uuid4()
    notice = BudgetNotice(
        tenant_id=tenant_id,
        workspace_id="guild",
        platform="discord",
        channel_id="channel",
        recipient_ids=(),
        limit_line="$5.00",
        monthly=False,
        budget_id=uuid.uuid4(),
        window_key="window",
    )
    assert await notice_recipients(sessionmaker, notice, None) == ("server-admin",)
    admins.assert_awaited_once()
    assert (
        await support_recipient_tiers(
            sessionmaker,
            tenant_id=tenant_id,
            platform="discord",
            channel_id="channel",
            requester_id="requester",
        )
        == ()
    )


async def test_grant_recipients_and_writes_are_disabled_without_database_access(
    admins_off: None,
) -> None:
    sessionmaker = AsyncMock()
    assert (
        await channel_admin_user_ids(
            sessionmaker, tenant_id=uuid.uuid4(), platform="discord", channel_id="channel", limit=5
        )
        is None
    )
    sessionmaker.assert_not_called()
    session = AsyncMock()
    assert await list_channel_admins(session, tenant_id=uuid.uuid4(), platform="discord") == []
    assert (
        await get_channel_admins(
            session, tenant_id=uuid.uuid4(), platform="discord", channel_id="channel"
        )
        is None
    )
    assert (
        await list_administered_channel_ids(
            session,
            tenant_id=uuid.uuid4(),
            platform="discord",
            platform_user_id="user",
            role_ids=("role",),
        )
        == frozenset()
    )
    with pytest.raises(ValueError, match="Channel admins are disabled"):
        await set_channel_admins(
            session,
            tenant_id=uuid.uuid4(),
            platform="discord",
            channel_id="channel",
            role_ids=(),
            user_ids=("user",),
            actor_account_id=None,
        )
    with pytest.raises(ValueError, match="Channel admins are disabled"):
        await delete_channel_admins(
            session, tenant_id=uuid.uuid4(), platform="discord", channel_id="channel"
        )
    session.execute.assert_not_called()


def test_switch_is_read_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_channel_admins_enabled()
    monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "false")
    assert not channel_admins_enabled()
    monkeypatch.setenv("DAIMON_CHANNEL_ADMINS__ENABLED", "true")
    assert not channel_admins_enabled()
    reset_channel_admins_enabled()
    assert channel_admins_enabled()
    reset_channel_admins_enabled()


def test_supplied_settings_override_environment_for_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_channel_admins_enabled()
    monkeypatch.delenv("DAIMON_CHANNEL_ADMINS__ENABLED", raising=False)
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        channel_admins={"enabled": False},  # type: ignore[arg-type]
    )
    prime_channel_admins_enabled(settings)
    try:
        assert not channel_admins_enabled()
        grant = ChannelAdminsRow(
            tenant_id=uuid.uuid4(),
            platform="discord",
            channel_id="channel",
            role_ids=(),
            user_ids=("user",),
            updated_by_account_id=None,
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert not is_channel_admin(ChannelAdminCaller(platform_user_id="user"), grant=grant)
    finally:
        reset_channel_admins_enabled()
