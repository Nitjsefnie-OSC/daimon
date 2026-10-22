"""An admin's channel switch previews, confirms on the card and compare-and-sets once."""

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from daimon.core.authz import Subject
from daimon.core.channel_backend_switch import (
    ChannelSwitchRefused,
    apply_channel_backend_switch,
    preview_channel_backend_switch,
)
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.tool_safety import (
    CONFIRMED_DAIMON_TOOLS,
    DAIMON_SERVER_NAME,
    ToolCall,
    ToolSafetyPolicy,
    backend_switch_tools,
    decide_tool_call,
)
from mux.profiles import selectable_models
from pydantic import ValidationError

ADMIN = Subject(is_admin=True, platform_user_id="requester")


def _call(**changes: object) -> ToolCall:
    data: dict[str, object] = {
        "channel_id": "channel",
        "target_backend": "gemini",
        "model": "gemini-4-argon",
        "expected_revision": 0,
        "preview_digest": "a" * 64,
    }
    data.update(changes)
    return ToolCall(
        tool_use_id="channel-switch-test",
        server_name=DAIMON_SERVER_NAME,
        tool_name="request_channel_backend_switch",
        input=data,
    )


def test_channel_switch_backends_are_the_default_then_text_only_profiles() -> None:
    from mux.profiles import channel_switch_backends, channel_switch_profile

    assert channel_switch_backends() == ("anthropic", "openai", "gemini")
    assert channel_switch_profile("gemini") == "gemini.inline_reuse"
    assert channel_switch_profile("openai") == "openai.conversation_only"
    assert channel_switch_profile("selfhosted") is None


def test_selectable_models_put_the_default_first() -> None:
    assert selectable_models("gemini") == (
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-4-argon",
    )
    assert selectable_models("openai") == ("gpt-6-luna",)


def test_the_channel_switch_waits_for_the_card_like_the_agent_switch() -> None:
    assert CONFIRMED_DAIMON_TOOLS["request_channel_backend_switch"] == "preview_digest"
    assert "request_channel_backend_switch" in backend_switch_tools(enabled=True)
    assert backend_switch_tools(enabled=False) == frozenset()
    policy = ToolSafetyPolicy(enabled=True, unattended_writes=("*",))
    trusted = frozenset({DAIMON_SERVER_NAME})
    selected = backend_switch_tools(enabled=True)

    def outcome(call: ToolCall, *, attended: bool) -> str:
        return decide_tool_call(
            policy, call, attended=attended, trusted_servers=trusted, confirmed_tools=selected
        ).outcome

    assert outcome(_call(preview_digest=None), attended=True) == "allow"
    assert outcome(_call(), attended=True) == "ask"
    assert outcome(_call(), attended=False) == "deny"


def test_the_card_binds_only_the_reference_never_model_text() -> None:
    prompt = prompt_for_tool_call(
        _call(cost_line="Free", summary="Everything carries over"),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    assert prompt.admin is True and prompt.backend_switch is None
    assert prompt.channel_backend_switch is not None
    assert prompt.channel_backend_switch.model_dump() == {
        "channel_id": "channel",
        "target_backend": "gemini",
        "model": "gemini-4-argon",
        "expected_revision": 0,
        "preview_digest": "a" * 64,
    }
    assert "Free" not in prompt.model_dump_json()
    assert "carries over" not in prompt.model_dump_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"channel_id": ""},
        {"channel_id": None},
        {"target_backend": "unknown"},
        {"expected_revision": -1},
        {"expected_revision": "0"},
        {"preview_digest": "bad"},
    ],
)
def test_malformed_channel_reference_never_creates_a_card(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        prompt_for_tool_call(
            _call(**changes), requester_platform_user_id="requester", now=datetime.now(UTC)
        )


@pytest.fixture
async def tenant_channel(db_preflight, db_committing_engine):
    from daimon.core.channel_backend import BackendActor, make_backend_service
    from daimon.core.config import TurnSettings
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.factories import make_account, make_platform_principal, make_tenant
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        pytest.skip("Backend control proofs require an owned DB or isolated CI")
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, tenant=tenant, account=account, platform="discord", external_id="requester"
        )
    service = make_backend_service(sessions, TurnSettings(path="mux", channel_backends=True))
    actor = BackendActor(tenant.id, account.id, Subject(is_admin=True))

    async def allow(*backends: str) -> None:
        policy = await service.tenant_policy(tenant.id)
        await service.set_tenant_allowlist(tenant.id, backends, policy.revision, actor)

    await allow("anthropic", "openai", "gemini")
    yield sessions, tenant, account, allow


async def _preview(sessions, tenant, target="gemini", model=None, channel="channel"):
    return await preview_channel_backend_switch(
        sessions,
        tenant_id=tenant.id,
        platform="discord",
        channel_id=channel,
        target_backend=target,
        model=model,
    )


async def _apply(sessions, tenant, account, preview, *, subject=ADMIN, digest=None, revision=None):
    return await apply_channel_backend_switch(
        sessions,
        tenant_id=tenant.id,
        account_id=account.id,
        subject=subject,
        platform="discord",
        channel_id=preview.channel_id,
        target_backend=preview.target_backend,
        model=preview.model if preview.target_backend != "anthropic" else None,
        expected_revision=preview.expected_revision if revision is None else revision,
        expected_preview_digest=preview.digest if digest is None else digest,
    )


async def test_never_configured_channel_previews_the_default_and_text_only(tenant_channel):
    sessions, tenant, _account, _allow = tenant_channel
    flash = await _preview(sessions, tenant)
    assert flash.allowed and flash.refusal is None
    assert (flash.current_backend, flash.current_model, flash.expected_revision) == (
        "anthropic",
        None,
        0,
    )
    assert flash.model == "gemini-3.8-flash"
    assert flash.models == ("gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-4-argon")
    assert "memory" in flash.not_carried and "tools" in flash.not_carried
    assert "Text only" in flash.carries_over_line
    argon = await _preview(sessions, tenant, model="gemini-4-argon")
    assert argon.allowed and argon.model == "gemini-4-argon"
    assert argon.digest != flash.digest
    assert (await _preview(sessions, tenant, model="gemini-4-argon")).digest == argon.digest
    luna = await _preview(sessions, tenant, target="openai")
    assert luna.allowed and luna.model == "gpt-6-luna"


@pytest.mark.parametrize(
    ("target", "model", "words"),
    [
        ("gemini", "gemini-3.8-pro", "isn't available on gemini"),
        ("openai", "gemini-4-argon", "isn't available on openai"),
        ("anthropic", "claude-opus-5-5", "without a model"),
        ("selfhosted", None, "Pick one of: anthropic, openai, gemini"),
    ],
)
async def test_unselectable_choices_preview_as_refused(tenant_channel, target, model, words):
    sessions, tenant, account, _allow = tenant_channel
    preview = await _preview(sessions, tenant, target=target, model=model)
    assert not preview.allowed and words in (preview.refusal or "")
    with pytest.raises(ChannelSwitchRefused):
        await _apply(sessions, tenant, account, preview)


async def test_a_provider_the_tenant_has_not_enabled_is_refused(tenant_channel):
    sessions, tenant, account, allow = tenant_channel
    await allow("anthropic")
    preview = await _preview(sessions, tenant)
    assert not preview.allowed and "isn't turned on" in (preview.refusal or "")
    with pytest.raises(ChannelSwitchRefused):
        await _apply(sessions, tenant, account, preview)


async def test_confirmed_switch_writes_one_channel_revision_then_clears(tenant_channel):
    from daimon.core.channel_backend import channel_ref, current_backend
    from daimon.core.stores import mux_state
    from daimon.core.stores.security_audit import list_events

    sessions, tenant, account, _allow = tenant_channel
    preview = await _preview(sessions, tenant, model="gemini-4-argon")
    applied, revision = await _apply(sessions, tenant, account, preview)
    assert applied == preview
    assert (revision.backend, revision.profile, revision.model, revision.local) == (
        "gemini",
        "gemini.inline_reuse",
        "gemini-4-argon",
        1,
    )
    channel = channel_ref(tenant.id, "discord", "channel")
    async with sessions() as session:
        assert await current_backend(session, channel) == revision
        assert await mux_state.config_origin(session, channel, 1) == "channel"
        other = await current_backend(session, channel_ref(tenant.id, "discord", "other"))
    assert other is None
    # The same confirmation applies once: the channel moved on.
    with pytest.raises(ChannelSwitchRefused, match="Preview again"):
        await _apply(sessions, tenant, account, preview)
    now = await _preview(sessions, tenant, target="anthropic")
    assert (now.current_backend, now.current_model, now.expected_revision) == (
        "gemini",
        "gemini-4-argon",
        1,
    )
    assert now.not_carried == ()
    _, cleared = await _apply(sessions, tenant, account, now)
    async with sessions() as session:
        assert await mux_state.config_origin(session, channel, cleared.local) == "channel_clear"
    back = await _preview(sessions, tenant)
    assert (back.current_backend, back.expected_revision) == ("anthropic", cleared.local)
    async with sessions() as session:
        events = await list_events(session, tenant_id=tenant.id)
    switches = [event for event in events if event.tool_name == "channel_backend"]
    assert sorted(event.operation for event in switches) == ["clear", "set"]
    assert all(event.account_id == account.id for event in switches)


@pytest.mark.parametrize("change", ["digest", "revision", "member", "agent_key"])
async def test_changed_previews_and_non_admins_never_write(tenant_channel, change):
    from daimon.core.channel_backend import channel_ref, current_backend

    sessions, tenant, account, _allow = tenant_channel
    preview = await _preview(sessions, tenant)
    kwargs: dict[str, Any] = {
        "digest": {"digest": "b" * 64},
        "revision": {"revision": 1},
        "member": {"subject": Subject(platform_user_id="requester")},
        "agent_key": {"subject": Subject(is_admin=True, via_agent_key=True)},
    }[change]
    with pytest.raises(ChannelSwitchRefused):
        await _apply(sessions, tenant, account, preview, **kwargs)
    async with sessions() as session:
        assert await current_backend(session, channel_ref(tenant.id, "discord", "channel")) is None


async def test_the_card_reloads_the_server_preview_and_rechecks_the_clicker(tenant_channel):
    from unittest.mock import AsyncMock

    from daimon.core.channel_backend import make_backend_service
    from daimon.core.config import TurnSettings
    from daimon.core.confirmation import backend_switch_callbacks

    sessions, tenant, account, allow = tenant_channel
    preview = await _preview(sessions, tenant, model="gemini-4-argon")
    prompt = prompt_for_tool_call(
        _call(preview_digest=preview.digest),
        requester_platform_user_id="requester",
        now=datetime.now(UTC),
    )
    admin = AsyncMock(return_value=True)
    prepare, check = backend_switch_callbacks(
        make_backend_service(sessions, TurnSettings(path="mux", channel_backends=True)),
        sessionmaker=sessions,
        anthropic=cast(Any, None),
        tenant_id=tenant.id,
        account_id=account.id,
        current_admin=admin,
    )
    shown = await prepare(prompt)
    assert shown.title == "Switch this channel to gemini (gemini-4-argon)?"
    assert "Text only" in (shown.consequence or "")
    assert "Channel: channel" in shown.detail_lines
    assert await check(shown, "requester") is None
    assert await check(shown, "someone-else") is not None
    admin.return_value = False
    assert await check(shown, "requester") is not None
    admin.return_value = True
    await allow("anthropic")
    assert await check(shown, "requester") is not None
    with pytest.raises(ValueError, match="Preview"):
        await prepare(prompt)
