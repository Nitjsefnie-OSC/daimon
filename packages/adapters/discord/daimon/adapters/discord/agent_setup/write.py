"""Agent creation and inline PAT helpers for Discord."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_identity import queue_agent_face
from daimon.core.constants import ALLOWED_MODEL_IDS
from daimon.core.defaults.ma_index import (
    find_agents_by_daimon_tag,
)
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import AgentNameCollision, SpecError
from daimon.core.specs import (
    AgentSpec,
)
from pydantic import ValidationError

_log = structlog.get_logger()

if TYPE_CHECKING:
    pass


def validate_model_id(model: str) -> str | None:
    """Return an error message if `model` is not in the allow-list; None if valid.

    Discord modals cannot contain Select components, so we validate
    free-text input at submit time against ALLOWED_MODEL_IDS.
    """
    if model not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        return f"Model `{model}` is not allowed. Choose one of: {allowed}"
    return None


async def load_agent_inline_pat(runtime: DiscordRuntime, *, agent_id: uuid.UUID) -> str | None:
    return None


async def create_blank_agent(
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    name: str,
    system: str | None,
    model: str,
    account_id: uuid.UUID,
) -> ResourceOutcome:
    """Build a blank AgentSpec from modal fields, reconcile, and start its face.

    Tenant-scoped name uniqueness: rejects if `name` already exists anywhere in
    this tenant, regardless of owner. Agent names are tenant-wide identity —
    reconcile dedup and the resolver key on (tenant, name) only.
    """
    collisions = await find_agents_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if collisions:
        raise AgentNameCollision(
            f"This server already has an agent named **{name}**. Pick a different name."
        )
    try:
        spec = AgentSpec.model_validate({"name": name, "model": model, "system": system})
    except ValidationError as err:
        raise SpecError(f"Spec validation failed: {err}") from err
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    outcome = await reconcile_agent(
        runtime.anthropic,
        spec,
        tenant_id=tenant_id,
        dry_run=False,
        account_id=account_id,
        public_url=public_url,
        # New agents created from the panel are user-owned, NOT seeded
        # resources — managed=True would stamp daimon_managed=true and make
        # them sweep-eligible, so the next defaults apply (every boot/deploy)
        # archives them because they aren't in the seeded spec list.
        managed=False,
        references=runtime.sessionmaker,
    )
    if outcome.anthropic_id is not None:
        queue_agent_face(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            agent_name=name,
            metadata=None,  # managed=False above: only the default name can make it built-in
            default_agent_name=runtime.deployment_default.agent_name,
        )
    return outcome



async def store_inline_pat(
    runtime: DiscordRuntime,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    plaintext_pat: str,
) -> str:
    raise ValueError("GitHub repository access requires a GitHub App grant.")
