"""An admin's chat request to point one channel at another model provider.

The channel-wide counterpart of the agent switch (`AgentBackendService`). A
preview reads what the channel runs now, what it would run and what does not
carry over; the write is a compare-and-set against that preview through
`set_channel_backend` / `clear_channel_backend`, the same owners the CLI and
admin bindings use. Nothing changes until an admin confirms the card, and a
channel nobody switches keeps the default.

A switched channel's turns run the deployment's provider runtime for that
profile (`daimon.core.turn.runtime_manifest`): text only, without the agent's
tools, skills, connections or memory. The preview says so before anyone
confirms.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Final

from daimon.core.access_policy import OPEN_ACCESS_POLICY
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_backend import (
    BackendUnsupported,
    channel_ref,
    check_backend,
    clear_channel_backend,
    current_backend,
    set_channel_backend,
)
from daimon.core.errors import DaimonError
from daimon.core.pricing import MODEL_PRICING, list_price_preview, provider_prices
from daimon.core.stores import access_policy as policy_store
from daimon.core.stores import agent_backends, mux_state, security_audit
from mux.contracts.config import (
    DEFAULT_BACKEND,
    BackendConfig,
    ConfigRevision,
    ResolvedBackend,
    resolve_default,
)
from mux.contracts.ids import ChannelRef, Provider
from mux.errors import InvalidConfig
from mux.profiles import channel_switch_backends, channel_switch_profile, selectable_models
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "ChannelBackendPreview",
    "ChannelSwitchRefused",
    "apply_channel_backend_switch",
    "preview_channel_backend_switch",
]

#: What a channel switched off the default no longer has, in card wording.
_NOT_CARRIED: Final[tuple[str, ...]] = ("tools", "skills", "connections", "memory", "files")


class ChannelSwitchRefused(DaimonError):
    """A channel switch that cannot be previewed or applied. Nothing changed."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason.replace("_", " "))


class ChannelBackendPreview(BaseModel):
    """Everything the card shows and the write compares, read from the server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    platform: str
    channel_id: str
    target_backend: Provider
    model: str | None
    models: tuple[str, ...]
    current_backend: Provider
    current_model: str | None
    expected_revision: int = Field(ge=0)
    allowed: bool
    refusal: str | None = None
    not_carried: tuple[str, ...]
    cost_line: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def target_label(self) -> str:
        if self.target_backend == DEFAULT_BACKEND:
            return f"the default ({DEFAULT_BACKEND.capitalize()})"
        return f"{self.target_backend} ({self.model})"

    @property
    def carries_over_line(self) -> str:
        if not self.not_carried:
            return "Each agent answers with its own model, tools, skills and connections again."
        return (
            "Text only here: " + ", ".join(self.not_carried) + " don't carry over. "
            "Switch back any time; nothing is deleted."
        )


def _selection(
    target: Provider, model: str | None
) -> tuple[ResolvedBackend | None, tuple[str, ...]]:
    """The channel configuration to write, or None to clear back to the default."""
    profile = channel_switch_profile(target)
    if target != DEFAULT_BACKEND and profile is None:
        raise ChannelSwitchRefused(
            "unknown_backend", "Pick one of: " + ", ".join(channel_switch_backends()) + "."
        )
    if target == DEFAULT_BACKEND or profile is None:
        if model is not None:
            raise ChannelSwitchRefused(
                "default_has_no_model",
                "The default uses each agent's own Claude model. Switch back without a model.",
            )
        return None, ()
    models = selectable_models(target)
    chosen = model or models[0]
    if chosen not in models:
        raise ChannelSwitchRefused(
            "model_not_selectable",
            f"{chosen} isn't available on {target} here. Pick one of: {', '.join(models)}.",
        )
    try:
        selection = resolve_default(BackendConfig(backend=target, profile=profile, model=chosen))
    except InvalidConfig as error:
        raise ChannelSwitchRefused("invalid_configuration", str(error)) from error
    return selection, models


def _cost_line(selection: ResolvedBackend | None) -> str:
    if selection is None or selection.model is None:
        return "Each agent's own Claude model and price apply."
    rates = provider_prices(selection.profile).get((selection.backend, selection.model))
    return list_price_preview(selection.model, rates or MODEL_PRICING.get(selection.model))


def _digest(facts: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


async def _current(
    session: AsyncSession, channel: ChannelRef
) -> tuple[ConfigRevision | None, Provider, str | None]:
    latest = await current_backend(session, channel)
    if latest is None or await mux_state.config_origin(session, channel, latest.local) in (
        "channel_clear",
        "agent",
    ):
        return latest, DEFAULT_BACKEND, None
    return latest, latest.backend, latest.model


async def _preview(
    session: AsyncSession, channel: ChannelRef, target: Provider, model: str | None
) -> tuple[ChannelBackendPreview, ResolvedBackend | None]:
    latest, current, current_model = await _current(session, channel)
    policy = await policy_store.load_access_policy(session, tenant_id=uuid.UUID(channel.tenant_id))
    selection: ResolvedBackend | None = None
    models: tuple[str, ...] = ()
    refusal: str | None = None
    try:
        selection, models = _selection(target, model)
        if selection is not None:
            check_backend(
                ConfigRevision.create(channel, 1, selection),
                enabled_backends=policy.enabled_backends,
            )
    except (ChannelSwitchRefused, BackendUnsupported) as error:
        refusal = str(error)
    facts: dict[str, object] = {
        "tenant_id": uuid.UUID(channel.tenant_id),
        "platform": channel.platform,
        "channel_id": channel.channel_id,
        "target_backend": target,
        "model": selection.model if selection is not None else None,
        "models": models,
        "current_backend": current,
        "current_model": current_model,
        "expected_revision": latest.local if latest is not None else 0,
        "allowed": refusal is None,
        "refusal": refusal,
        "not_carried": _NOT_CARRIED if target != DEFAULT_BACKEND else (),
        "cost_line": _cost_line(selection),
    }
    digest = _digest(
        {
            **facts,
            "current_digest": latest.digest if latest is not None else None,
            "enabled_backends": policy.enabled_backends,
            "selection": selection.content_digest() if selection is not None else None,
        }
    )
    return ChannelBackendPreview.model_validate({**facts, "digest": digest}), selection


async def preview_channel_backend_switch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    target_backend: Provider,
    model: str | None = None,
) -> ChannelBackendPreview:
    """Read-only: what switching `channel_id` to `target_backend` would do."""
    async with sessionmaker() as session:
        preview, _ = await _preview(
            session, channel_ref(tenant_id, platform, channel_id), target_backend, model
        )
    return preview


async def apply_channel_backend_switch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    subject: Subject,
    platform: str,
    channel_id: str,
    target_backend: Provider,
    model: str | None,
    expected_revision: int,
    expected_preview_digest: str,
) -> tuple[ChannelBackendPreview, ConfigRevision]:
    """Apply a confirmed preview, or refuse if anything it showed has changed."""
    place = Place(channel_id=channel_id)
    if not authorize(
        OPEN_ACCESS_POLICY, subject=subject, action=Action.SET_CHANNEL_RULE, place=place
    ):
        raise ChannelSwitchRefused("admin_required", "Only a server admin can switch a channel.")
    channel = channel_ref(tenant_id, platform, channel_id)
    async with policy_store.policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
        policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
        if not authorize(policy, subject=subject, action=Action.SET_CHANNEL_RULE, place=place):
            raise ChannelSwitchRefused(
                "admin_required", "Only a server admin can switch a channel."
            )
        if not await agent_backends.actor_exists(session, tenant_id, account_id):
            raise ChannelSwitchRefused("actor_not_found", "This account is unavailable here.")
        preview, selection = await _preview(session, channel, target_backend, model)
        if (
            preview.digest != expected_preview_digest
            or preview.expected_revision != expected_revision
        ):
            raise ChannelSwitchRefused(
                "stale_preview", "The channel changed since the preview. Preview again."
            )
        if not preview.allowed:
            raise ChannelSwitchRefused("not_allowed", preview.refusal)
        if selection is None:
            revision = await clear_channel_backend(session, channel)
        else:
            revision = await set_channel_backend(
                session,
                channel,
                BackendConfig(
                    backend=selection.backend, profile=selection.profile, model=selection.model
                ),
            )
        await security_audit.append_event(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=None,
            platform=platform,
            platform_user_id=subject.platform_user_id,
            tool_name="channel_backend",
            operation="clear" if selection is None else "set",
            outcome="allowed",
            reason="completed; "
            + (f"{selection.backend}:{selection.model}" if selection else DEFAULT_BACKEND),
            scope=f"channel_backend:{platform}:{channel_id}:{revision.local}",
        )
    return preview, revision
