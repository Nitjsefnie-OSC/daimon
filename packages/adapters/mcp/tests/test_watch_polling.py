"""Owner authorization, executor gating and management never trust poll results."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools.watches import manage_watch_impl, rotate_watch_impl
from daimon.adapters.mcp.watch_polling import poll_read_place as real_poll_read_place
from daimon.adapters.mcp.watch_polling import run_poll
from daimon.core.authz import Place
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.membership import Membership
from daimon.core.stores import watches
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.watch_fixtures import install_poll_ports
from fastmcp.exceptions import ToolError
from sqlalchemy import text

pytestmark = pytest.mark.fresh_schema


@pytest.fixture
async def runtime_watch(db_session_factory, monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        await install_poll_ports(db)
        await watches.set_watch_control(db, scope="global", enabled=True)
        tenant = await make_tenant(db)
        principal = await get_or_create_platform_principal(
            db, tenant_id=tenant.id, platform="discord", external_id="123"
        )
        await create_thread_session(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="4242",
            channel_id="4241",
            account_id=principal.account_id,
            ma_session_id="session-bound",
            ma_agent_id="agent-bound",
        )
        binding = ContinuationRequest(
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="4241",
            thread_id="4242",
            requester_account_id=principal.account_id,
            requester_external_user_id="123",
            target_ma_agent_id="agent-bound",
            target_name="analyst",
            requested_work=None,
            reason="watch_fired",
            idempotency_key=uuid.uuid4(),
        )
        watch = await watches.register_poll_watch(
            db,
            binding,
            instructions="Report changes.",
            tool="read_thread",
            arguments={"thread_id": "4242"},
            projection=".rows",
            rule="change",
            interval=60,
            expires_at=now + timedelta(days=1),
            now=now,
        )
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Membership(is_member=True)))
    runtime = SimpleNamespace(
        session_factory=db_session_factory,
        client=object(),
        membership_resolver=resolver,
        settings=SimpleNamespace(
            mcp=SimpleNamespace(public_url="https://os.example/mcp"), direct_message_policies={}
        ),
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.watch_polling.retrieve_agent",
        AsyncMock(return_value=ma_agent(id="agent-bound", name="analyst")),
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.watch_polling.poll_read_place",
        AsyncMock(return_value=Place(channel_id="4242", parent_channel_id="4241")),
    )
    auth = AuthIdentity(
        account_id=watch.owner_account_id,
        tenant_id=watch.tenant_id,
        platform="discord",
        platform_user_id="123",
        external_id=tenant.external_id,
        role=Role.USER,
    )
    return runtime, watch, auth


async def lease(runtime, watch):
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET next_check_at=now()-interval '1 second' WHERE id=:id"),
            {"id": watch.id},
        )
        return (await watches.claim_poll_watches(db))[0]["lease"]


async def test_polls_recheck_live_owner_before_every_read_and_cancel_revoked_wakes(runtime_watch):
    runtime, watch, _ = runtime_watch
    source = AsyncMock(return_value=SimpleNamespace(model_dump=lambda **_: {"rows": [1]}))
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.return_value = SimpleNamespace(model_dump=lambda **_: {"rows": [2]})
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        runtime.membership_resolver.resolve.return_value = Membership(is_member=False)
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
    assert source.await_count == 2
    assert all(
        call.kwargs["require_fresh"] for call in runtime.membership_resolver.resolve.await_args_list
    )
    async with runtime.session_factory() as db:
        current = await watches.get_watch(db, watch.id)
        assert current.status == "paused"
        evidence = await watches.list_evidence(db, watch.id)
        assert len(evidence) == 1
        assert (
            await get_continuation(db, idempotency_key=evidence[0].wake_key)
        ).status == "cancelled"


async def test_revocation_during_source_read_discards_result(runtime_watch):
    runtime, watch, _ = runtime_watch

    async def revoked_read(*args, **kwargs):
        runtime.membership_resolver.resolve.return_value = Membership(is_member=False)
        return SimpleNamespace(model_dump=lambda **_: {"rows": ["untrusted result"]})

    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", revoked_read):
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
    async with runtime.session_factory() as db:
        current = await watches.get_watch(db, watch.id)
        assert current.baseline == {} and current.status == "paused"
        assert await watches.list_evidence(db, watch.id) == []


async def test_producer_setting_off_never_calls_source(runtime_watch, monkeypatch):
    runtime, watch, _ = runtime_watch
    token = await lease(runtime, watch)
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "false")
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", AsyncMock()) as source:
        assert not await run_poll(runtime, watch.id, token)
        source.assert_not_called()


@pytest.mark.parametrize("action", ["show", "pause", "resume", "delete"])
async def test_management_refuses_nonowner_and_cross_tenant(runtime_watch, action):
    runtime, watch, auth = runtime_watch
    from dataclasses import replace

    for forged in (replace(auth, account_id=uuid.uuid4()), replace(auth, tenant_id=uuid.uuid4())):
        with pytest.raises(ToolError, match="not found"):
            await manage_watch_impl(runtime, forged, watch_id=str(watch.id), action=action)


async def test_owner_can_pause_show_resume_and_delete(runtime_watch):
    runtime, watch, auth = runtime_watch
    assert (
        await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="pause")
    ).status == "paused"
    assert (
        await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="show")
    ).status == "paused"
    assert (
        await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="resume")
    ).status == "active"
    assert (
        await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="delete")
    ).status == "deleted"


async def test_page_key_is_delivered_privately_once_never_in_mcp_result(runtime_watch):
    runtime, watch, auth = runtime_watch
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET source='page_event' WHERE id=:id"), {"id": watch.id}
        )
    deliver = AsyncMock()
    with patch("daimon.adapters.mcp.tools.watches.private_key_delivery", deliver):
        response = await rotate_watch_impl(runtime, auth, watch_id=str(watch.id))
    deliver.assert_awaited_once()
    key = deliver.await_args.kwargs["key"]
    assert len(key) >= 32 and key not in response.model_dump_json()
    shown = await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="show")
    assert key not in shown.model_dump_json()
    async with runtime.session_factory() as db:
        values = (
            (
                await db.execute(
                    text("SELECT digest FROM watch_capabilities WHERE watch_id=:id"),
                    {"id": watch.id},
                )
            )
            .scalars()
            .all()
        )
        assert key not in str(values) and len(values) == 1


async def test_failed_private_key_delivery_pauses_watch(runtime_watch):
    runtime, watch, auth = runtime_watch
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET source='page_event' WHERE id=:id"), {"id": watch.id}
        )
    with (
        patch(
            "daimon.adapters.mcp.tools.watches.private_key_delivery",
            AsyncMock(side_effect=RuntimeError),
        ),
        pytest.raises(ToolError, match="watch paused"),
    ):
        await rotate_watch_impl(runtime, auth, watch_id=str(watch.id))
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "paused"


async def test_revoked_exact_destination_stops_poll_before_source(runtime_watch):
    runtime, watch, _ = runtime_watch
    with (
        patch(
            "daimon.adapters.mcp.watch_polling.poll_read_place",
            AsyncMock(side_effect=ToolError("missing thread access")),
        ),
        patch("daimon.adapters.mcp.watch_polling._read_thread_impl", AsyncMock()) as source,
    ):
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.assert_not_called()
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "paused"


async def test_owner_registration_binds_attended_origin_and_refuses_unattended(runtime_watch):
    from dataclasses import replace

    from daimon.adapters.mcp.tools.watches import create_watch_impl
    from daimon.core.turn_origin import turn_origin

    runtime, watch, auth = runtime_watch
    auth = replace(auth, chat_session_id="test-control-session")
    for attended in (True, False):
        async with turn_origin(
            runtime.session_factory,
            tenant_id=watch.tenant_id,
            account_id=watch.owner_account_id,
            platform="discord",
            parent_channel_id=watch.parent_channel_id,
            thread_id=watch.thread_id,
            responder_ma_agent_id=watch.agent_id,
            responder_name=watch.agent_name,
            role=Role.USER,
            chat_session_id=auth.chat_session_id,
            attended=attended,
        ) as origin:
            if attended:
                created = await create_watch_impl(
                    runtime,
                    auth,
                    origin_context_id=str(origin.id),
                    instructions="Report a change.",
                    source="poll",
                    tool="read_thread",
                    arguments={"thread_id": "4242"},
                )
                assert created.thread_id == watch.thread_id and created.agent_id == watch.agent_id
            else:
                with pytest.raises(ToolError, match="attended"):
                    await create_watch_impl(
                        runtime,
                        auth,
                        origin_context_id=str(origin.id),
                        instructions="Report a change.",
                        source="poll",
                    )


@pytest.mark.parametrize("rule_kind", ["ask", "deny"])
async def test_live_agent_tool_rule_stops_polling(runtime_watch, rule_kind):
    from daimon.core.authz import Subject
    from daimon.core.stores.agent_tool_rules import set_rule

    runtime, watch, _ = runtime_watch
    async with runtime.session_factory.begin() as db:
        await set_rule(
            db,
            tenant_id=watch.tenant_id,
            agent_id=watch.agent_id,
            agent_name=watch.agent_name,
            tool_key="daimon-mcp/read_thread",
            server_url="https://os.example/mcp",
            rule=rule_kind,
            set_by="admin",
            actor=Subject(is_admin=True),
        )
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", AsyncMock()) as source:
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.assert_not_called()


async def test_current_agent_policy_denial_prevents_source_read(runtime_watch):
    from daimon.core.access_policy import AgentRule, TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy

    runtime, watch, _ = runtime_watch
    async with runtime.session_factory.begin() as db:
        await set_access_policy(
            db,
            tenant_id=watch.tenant_id,
            policy=TenantAccessPolicy(agent_rules={"analyst": AgentRule(runs_in=())}),
        )
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", AsyncMock()) as source:
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.assert_not_called()


@pytest.mark.parametrize("history_allowed", [True, False])
async def test_poll_requires_owners_native_history_permission(
    runtime_watch, monkeypatch, history_allowed
):
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    import discord

    runtime, _, auth = runtime_watch
    # Capture the real implementation, replacing only the fixture's high-level stub.
    from daimon.adapters.mcp import watch_polling as module

    real_place = real_poll_read_place
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.category_id = 4242, None
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, read_message_history=history_allowed
    )
    member = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False))

    @asynccontextmanager
    async def client(_):
        yield object()

    monkeypatch.setattr(module, "rest_client", client)
    monkeypatch.setattr(module, "_require_bot_token", lambda _: "fake")
    monkeypatch.setattr(module, "_resolve_member", AsyncMock(return_value=(object(), member)))
    monkeypatch.setattr(module, "_resolve_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(module, "_require_guild_channel", lambda raw, _: raw)
    if history_allowed:
        assert (await real_place(runtime, auth, "4242")).channel_id == "4242"
    else:
        with pytest.raises(module.PollAccessRevoked, match="history"):
            await real_place(runtime, auth, "4242")


async def test_invalid_initial_rule_shape_backs_off_without_poisoning_baseline(runtime_watch):
    runtime, watch, _ = runtime_watch
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET rule=CAST(:rule AS jsonb) WHERE id=:id"),
            {"id": watch.id, "rule": '{"projection":".rows","kind":"added"}'},
        )
    source = AsyncMock(return_value=SimpleNamespace(model_dump=lambda **_: {"rows": None}))
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
    async with runtime.session_factory() as db:
        row = await watches.get_watch(db, watch.id)
        assert row.baseline == {} and row.consecutive_errors == 1


async def test_owner_account_tenant_change_stops_poll_before_read(runtime_watch):
    runtime, watch, _ = runtime_watch
    token = await lease(runtime, watch)
    async with runtime.session_factory.begin() as db:
        other = await make_tenant(db)
        await db.execute(
            text("UPDATE accounts SET tenant_id=:tenant WHERE id=:owner"),
            {"tenant": other.id, "owner": watch.owner_account_id},
        )
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", AsyncMock()) as source:
        assert not await run_poll(runtime, watch.id, token)
        source.assert_not_called()
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "paused"
