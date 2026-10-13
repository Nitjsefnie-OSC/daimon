"""Credential-reference validation without credential discovery or SDK imports."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self, cast
from uuid import UUID

from daimon.core.errors import TurnError
from daimon.core.neutral_resources import (
    WorkerPermissions,
    WorkerSpecRefused,
    freeze_root_permissions,
)
from daimon.core.turn.ceiling import remaining_s, turn_deadline
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.config import CapabilityRequirement, ConfigRevision, ThreadMode
from mux.contracts.ids import Scope
from mux.contracts.profile import Capability
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ProviderBinding, ResourceBinding
from mux.errors import UnsupportedCapability
from mux.profiles import backend_selection, get_profile, native_agent_tools
from mux.state.operations import OperationRecord
from mux.state.store import binding_slot
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

if TYPE_CHECKING:
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.neutral_resources import NeutralResourceHost, ResolvedWorkerSpec
    from daimon.core.stores.domain import BackendAgent, TaskContinuationRow
    from daimon.core.stores.neutral_catalog import AgentRevision
    from daimon.core.turn.deps import TurnDeps
    from daimon.core.turn.persistence import TurnPersistence
    from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest

_RESERVED = ("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_")
_ROWS = TypeAdapter(list[dict[str, JsonValue]])
_MAX_BYTES = 1_048_576


class _FrozenRoot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_id: UUID
    origin_id: UUID
    binding: ProviderBinding
    permissions: WorkerPermissions


class _ResumedRoot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_id: UUID
    origin_id: UUID
    root_origin_id: UUID
    run_id: UUID
    wake_id: UUID
    wake_key: UUID
    wake_owner: str
    wake_attempt: int
    wake_started_at: datetime
    scope: Scope
    binding: ProviderBinding


async def _root_context(
    deps: TurnDeps,
    root: _FrozenRoot,
    *,
    now: datetime,
    resumed: _ResumedRoot | None = None,
    execution_origin: bool = False,
) -> TenantAccessPolicy:
    from daimon.core.stores import turn_card_intents, turn_origins
    from daimon.core.stores.access_policy import load_access_policy
    from daimon.core.stores.accounts import get_account_with_tenant

    parent, binding = root.permissions, root.binding
    facts, scope = parent.fleet, parent.fleet.scope
    async with deps.sessionmaker() as db:
        origin = await turn_origins.get_active_origin(
            db,
            origin_id=root.origin_id,
            tenant_id=UUID(scope.tenant_id),
            account_id=facts.account_id,
            platform=binding.thread.channel.platform,
            now=now,
        )
        intent = await turn_card_intents.get_turn_card_intent(db, id=root.intent_id)
        account = await get_account_with_tenant(db, account_id=facts.account_id)
        policy = await load_access_policy(db, tenant_id=UUID(scope.tenant_id))
        if resumed is not None:
            execution_intent = await turn_card_intents.get_turn_card_intent(
                db, id=resumed.intent_id
            )
            execution = (
                await turn_origins.get_active_origin(
                    db,
                    origin_id=resumed.origin_id,
                    tenant_id=UUID(scope.tenant_id),
                    account_id=facts.account_id,
                    platform=binding.thread.channel.platform,
                    now=now,
                )
                if execution_origin
                else None
            )
            if (
                resumed.root_origin_id != root.origin_id
                or resumed.run_id != facts.run_id
                or resumed.binding != binding
                or len(parent.source_agents) != 1
                or (resumed.scope.tenant_id, resumed.scope.account_id, resumed.scope.principal_id)
                != (scope.tenant_id, scope.account_id, scope.principal_id)
                or execution_intent is None
                or (
                    execution_intent.tenant_id,
                    execution_intent.platform,
                    execution_intent.channel_id,
                    execution_intent.thread_id,
                )
                != (
                    UUID(scope.tenant_id),
                    binding.thread.channel.platform,
                    facts.root.channel.channel_id,
                    facts.root.thread_id,
                )
                or execution_intent.status not in ("prepared", "posted")
                or (
                    execution_origin
                    and (
                        execution is None
                        or execution.is_external
                        or execution.is_setup
                        or execution.responder_ma_agent_id != parent.source_agents[0].id
                        or execution.parent_channel_id != facts.root.channel.channel_id
                        or execution.thread_id != facts.root.thread_id
                    )
                )
            ):
                raise WorkerSpecRefused("origin", "stale")
    if not (origin and intent and account and deps.state_store):
        raise WorkerSpecRefused("origin", "stale")
    if (
        facts.depth != 0
        or len(parent.source_agents) != 1
        or not facts.enabled
        or not facts.active
        or facts.origin_id != root.origin_id
        or str(facts.account_id) != scope.account_id
        or facts.root != binding.thread
        or binding.legacy_account_id != scope.account_id
        or origin.responder_ma_agent_id != parent.source_agents[0].id
        or origin.parent_channel_id != facts.root.channel.channel_id
        or origin.thread_id != facts.root.thread_id
        or origin.is_external
        or (account.tenant_id, account.platform) != (origin.tenant_id, origin.platform)
        or account.platform_user_id != parent.subject.platform_user_id
        or account.is_external
        or intent.tenant_id != origin.tenant_id
        or intent.platform != origin.platform
        or intent.channel_id != origin.parent_channel_id
        or intent.thread_id != origin.thread_id
        or intent.status not in ("prepared", "posted")
        or parent.limits.deadline <= now
        or await deps.state_store.get_binding(binding_slot(binding)) != binding
    ):
        raise WorkerSpecRefused("origin", "stale")
    return policy


async def _reserve_root(
    deps: TurnDeps,
    persistence: TurnPersistence,
    intent_id: UUID,
    origin_id: UUID,
    _key: str,
    digest: str,
) -> OperationRecord:
    from daimon.core.stores import mux_state

    async with deps.sessionmaker.begin() as db:
        record = await mux_state.get_fleet_root_operation(
            db,
            persistence.scope,
            intent_id=intent_id,
            origin_id=origin_id,
            slot=persistence.slot,
            request_digest=digest,
        )
    if record is None:
        raise WorkerSpecRefused("origin", "unbound")
    return record


async def freeze_prepared_root(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    intent_id: UUID,
    now: Callable[[], datetime],
) -> None:
    """Retain the exact host-owned root under its input's existing mutation fence."""
    from daimon.core.stores import fleet
    from daimon.core.stores.access_policy import load_access_policy
    from daimon.core.turn.persistence import current_persistence
    from daimon.core.turn_origin import current_origin_id

    persistence, origin_id = current_persistence.get(), current_origin_id.get()
    grant, source = prepared.admission.grant, prepared._source  # pyright: ignore[reportPrivateUsage]
    loader = deps.fleet_root_grants
    if not (persistence and origin_id and grant and source and loader):
        raise WorkerSpecRefused("origin", "unbound")
    scope, binding = persistence.scope, persistence.binding
    if (
        not deps.fleet_enabled
        or prepared.admission.shared_owner is not None
        or binding.native_refs.get("session") != prepared.ma_session_id
        or prepared.admission.account_id != UUID(scope.account_id)
        or source.resource_scope != scope
        or not source.source
        or source.source.ref.id != prepared.admission.agent.id
        or binding.config_revision
        != (prepared.admission.backend_revision.local if prepared.admission.backend_revision else 0)
    ):
        raise WorkerSpecRefused("origin", "unbound")
    async with deps.sessionmaker() as db:
        policy = await load_access_policy(db, tenant_id=grant.tenant_id)
    permissions = freeze_root_permissions(
        await loader(grant, source),
        source,
        admission=grant,
        policy=policy,
        now=now(),
    )
    root = _FrozenRoot(
        intent_id=intent_id, origin_id=origin_id, binding=binding, permissions=permissions
    )

    async def validate() -> None:
        await _root_context(deps, root, now=now())

    async def retain(_key: str) -> _FrozenRoot:
        await validate()
        run = await fleet.create_run(
            deps.sessionmaker,
            tenant_id=grant.tenant_id,
            orchestrator_account_id=prepared.admission.account_id,
            orchestrator_session_id=prepared.ma_session_id,
            orchestrator_binding_id=binding.id,
            platform=binding.thread.channel.platform,
            channel_id=grant.channel_id,
            thread_id=binding.thread.thread_id,
            operation_key=f"fleet_root:{origin_id}",
            total_budget_usd=permissions.limits.budget_usd,
            deadline_at=permissions.limits.deadline,
            max_workers=deps.fleet_max_workers,
            created_by="host",
        )
        await validate()
        return root.model_copy(
            update={
                "permissions": replace(permissions, fleet=replace(permissions.fleet, run_id=run.id))
            }
        )

    await validate()
    await persistence.mutate(
        prepared.session_ref,
        "root",
        root.model_dump(mode="json", exclude={"permissions"}),
        retain,
        _FrozenRoot,
        lambda _: "processed",
        guard=validate,
        retry_failed=False,
        operation_key=f"fleet_root:{origin_id}",
        begin=partial(_reserve_root, deps, persistence, intent_id, origin_id),
    )


async def resume_prepared_root(
    deps: TurnDeps,
    prepared: PreparedTurn,
    row: TaskContinuationRow,
    *,
    intent_id: UUID,
    now: Callable[[], datetime],
) -> None:
    """Retain only a locator to the completion claim's original frozen authority."""
    from daimon.core.continuity.wakes import FrozenWorkerBatch, require_worker_finished_root
    from daimon.core.stores import mux_state
    from daimon.core.turn.persistence import current_persistence
    from daimon.core.turn_origin import current_origin_id

    persistence, origin_id = current_persistence.get(), current_origin_id.get()
    source, grant = prepared._source, prepared.admission.grant  # pyright: ignore[reportPrivateUsage]
    if not (deps.fleet_enabled and persistence and origin_id and source and grant):
        raise WorkerSpecRefused("origin", "unbound")
    if (
        prepared.admission.shared_owner is not None
        or prepared._scope != persistence.scope  # pyright: ignore[reportPrivateUsage]
        or source.resource_scope != persistence.scope
        or row.tenant_id != UUID(persistence.scope.tenant_id)
        or row.requester_account_id != UUID(persistence.scope.account_id)
        or prepared.session_account_id != prepared.admission.account_id
        or grant.subject.platform_user_id != row.requester_external_user_id
        or row.started_at is None
        or not row.lease_owner
    ):
        raise WorkerSpecRefused("origin", "unbound")

    async def claim() -> None:
        await require_worker_finished_root(
            deps.sessionmaker,
            row,
            session_id=prepared.ma_session_id,
            account_id=prepared.admission.account_id,
            agent_id=prepared.admission.agent.id,
            now=now(),
        )

    await claim()
    batch = FrozenWorkerBatch.model_validate_json(row.requested_work or "")
    async with deps.sessionmaker() as db:
        record = await mux_state.get_fleet_root_operation(
            db, persistence.scope, run_id=batch.run_id
        )
    if record is None or record.operation.status != "processed":
        raise WorkerSpecRefused("origin", "unbound")
    try:
        root = _FrozenRoot.model_validate_json(json.dumps(dict(record.result)))
    except ValidationError:
        raise WorkerSpecRefused("origin", "unbound") from None
    locator = _ResumedRoot(
        intent_id=intent_id,
        origin_id=origin_id,
        root_origin_id=root.origin_id,
        run_id=batch.run_id,
        wake_id=row.id,
        wake_key=row.idempotency_key,
        wake_owner=row.lease_owner,
        wake_attempt=row.attempts,
        wake_started_at=row.started_at,
        scope=persistence.scope,
        binding=persistence.binding,
    )

    async def validate() -> None:
        await claim()
        await read_frozen_root(
            deps,
            persistence.scope,
            origin_id=root.origin_id,
            run_id=batch.run_id,
            now=now(),
        )
        await _root_context(deps, root, now=now(), resumed=locator, execution_origin=True)

    async def retain(_key: str) -> _ResumedRoot:
        await validate()
        return locator

    await validate()
    await persistence.mutate(
        prepared.session_ref,
        "root",
        locator.model_dump(mode="json"),
        retain,
        _ResumedRoot,
        lambda _: "processed",
        guard=validate,
        retry_failed=False,
        operation_key=f"fleet_root:{origin_id}",
        begin=partial(_reserve_root, deps, persistence, intent_id, origin_id),
    )
    await validate()


async def read_frozen_root(
    deps: TurnDeps,
    scope: Scope,
    *,
    intent_id: UUID | None = None,
    origin_id: UUID | None = None,
    run_id: UUID | None = None,
    now: datetime,
) -> tuple[WorkerPermissions, TenantAccessPolicy]:
    """Load one exact retained root, with its original finite authority and current policy."""
    from daimon.core.stores import fleet, mux_state

    if not deps.fleet_enabled or deps.state_store is None:
        raise WorkerSpecRefused("origin", "unbound")
    async with deps.sessionmaker() as db:
        record = await mux_state.get_fleet_root_operation(
            db,
            scope,
            intent_id=intent_id,
            origin_id=origin_id,
        )
    if record is None or record.operation.status != "processed":
        raise WorkerSpecRefused("origin", "unbound")
    resumed = None
    try:
        if "root_origin_id" in record.result:
            resumed = _ResumedRoot.model_validate_json(json.dumps(dict(record.result)))
            if (
                (intent_id is not None and intent_id != resumed.intent_id)
                or (origin_id is not None and origin_id != resumed.origin_id)
                or record.slot != binding_slot(resumed.binding)
            ):
                raise WorkerSpecRefused("origin", "unbound")
            async with deps.sessionmaker() as db:
                record = await mux_state.get_fleet_root_operation(
                    db,
                    scope,
                    origin_id=resumed.root_origin_id,
                )
            if record is None or record.operation.status != "processed":
                raise WorkerSpecRefused("origin", "unbound")
        root = _FrozenRoot.model_validate_json(json.dumps(dict(record.result)))
    except ValidationError:
        raise WorkerSpecRefused("origin", "unbound") from None
    parent, binding = root.permissions, root.binding
    original = parent.fleet.scope
    if (original.tenant_id, original.account_id, original.principal_id) != (
        scope.tenant_id,
        scope.account_id,
        scope.principal_id,
    ) or record.slot != binding_slot(binding):
        raise WorkerSpecRefused("origin", "unbound")
    policy = await _root_context(
        deps,
        root,
        now=now,
        resumed=resumed,
        execution_origin=origin_id is not None,
    )
    selected = parent.fleet.run_id
    if run_id is not None and selected != run_id:
        raise WorkerSpecRefused("origin", "unbound")
    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=UUID(scope.tenant_id), run_id=selected
    )
    if (
        run.status not in ("running", "collecting")
        or run.orchestrator_session_id != binding.native_refs.get("session")
        or run.orchestrator_binding_id != binding.id
        or run.orchestrator_account_id != parent.fleet.account_id
        or run.platform != binding.thread.channel.platform
        or run.channel_id != binding.thread.channel.channel_id
        or run.thread_id != binding.thread.thread_id
        or run.total_budget_usd != parent.limits.budget_usd
        or run.deadline_at != parent.limits.deadline
    ):
        raise WorkerSpecRefused("origin", "stale")
    return parent, policy


class ProviderRuntimeConfigurationError(RuntimeError):
    """Static, typed operator errors; never include manifest content or paths."""

    def __init__(self, reason: Literal["reserved_key_reference", "invalid_manifest"]) -> None:
        self.reason = reason
        message = (
            "Provider runtime api_key_env uses a reserved provider prefix "
            "(OPENAI_, ANTHROPIC_, GOOGLE_, GEMINI_). Use a host-scoped name such as "
            "DAIMON_CHANNEL_OPENAI_KEY or DAIMON_CHANNEL_GEMINI_KEY."
            if reason == "reserved_key_reference"
            else "Provider runtime manifest must be a readable JSON list of objects, at most 1 MiB."
        )
        super().__init__(message)


def validate_api_key_env(value: str) -> str:
    """Refuse SDK ambient-variable namespaces before looking up any secret."""
    if value.upper().startswith(_RESERVED):
        raise ProviderRuntimeConfigurationError("reserved_key_reference")
    return value


ApiKeyEnvironment = Annotated[
    str,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
    AfterValidator(validate_api_key_env),
]


def validate_configured_runtime_manifest() -> None:
    """Check an explicitly configured manifest's references, without constructing plans.

    Settings loaders call this only with mux and channel backends enabled. Missing
    manifests retain the existing selected-turn refusal; no discovery happens here.
    Scope/digest, full provider policy and key availability remain admission checks.
    """
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not path:
        return
    try:
        with Path(path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ProviderRuntimeConfigurationError("invalid_manifest")
        rows = _ROWS.validate_json(raw)
    except (OSError, ValidationError):
        raise ProviderRuntimeConfigurationError("invalid_manifest") from None
    for row in rows:
        reference = row.get("api_key_env")
        if isinstance(reference, str):
            validate_api_key_env(reference)


class ConfigurationGrant(BaseModel):
    """Finite operator-authorized resolved fields; no wildcard defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    profiles: tuple[str, ...] = Field(min_length=1)
    models: tuple[str | None, ...] = Field(min_length=1)
    thread_modes: tuple[ThreadMode, ...] = Field(min_length=1)
    requires: dict[Capability, CapabilityRequirement]


def configuration_matches(
    revision: ConfigRevision, *, digest: str | None, grant: ConfigurationGrant | None
) -> bool:
    """Legacy exact grants remain exact; bounded grants authorize only declared fields."""
    if grant is None:
        return digest == revision.digest
    return (
        revision.profile in grant.profiles
        and revision.model in grant.models
        and revision.thread_mode in grant.thread_modes
        and dict(revision.requires) == grant.requires
    )


def manifest_configuration_matches(row: Mapping[str, JsonValue], revision: ConfigRevision) -> bool:
    """Match before any credential lookup; invalid grants refuse at the loader boundary."""
    raw = row.get("configuration_grant")
    grant = ConfigurationGrant.model_validate(raw) if raw is not None else None
    digest = row.get("config_digest")
    return configuration_matches(
        revision, digest=digest if isinstance(digest, str) else None, grant=grant
    )


def fixed_plan_origin_allowed(origin: str | None) -> bool:
    """Only channel-origin plans bypass the complete authorized-source proof."""
    return origin == "channel"


async def capture_preparation_source(request: ProviderPreparationRequest) -> BackendAgent:
    """Capture source facts; the existing proof and materializer validate them."""
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
    from daimon.core.sessions import project_session_resources

    source = request.source
    if source is None:
        source = await load_backend_agent(
            request.deps.anthropic,
            request.deps.sessionmaker,
            tenant_id=request.tenant_id,
            agent_name=(request.admission.agent.metadata or {}).get(
                MA_METADATA_KEY_NAME, request.admission.config.agent_name
            )
            or "",
            actual_agent=request.admission.agent,
        )
    return await project_session_resources(
        source,
        agent=request.admission.agent,
        environment=request.admission.environment,
        session_factory=request.deps.sessionmaker,
        tenant_id=request.tenant_id,
        agent_uuid=source.agent_id,
        account_id=(
            None if request.admission.shared_owner is not None else request.admission.account_id
        ),
        resource_scope=request.scope,
        memory_read_only=request.admission.memory_read_only,
        fernet=request.deps.fernet,
        channel_skills=request.admission.channel_skills,
        tool_safety=request.deps.tool_safety,
        asks_before_publishing=request.admission.asks_before_publishing,
        mcp_settings=request.deps.mcp,
        agent_github_app=request.deps.agent_github_app,
        is_external=request.admission.is_external,
        hide_personal_for=request.admission.shared_owner,
        slack_turn_context_id=request.admission.slack_turn_context_id,
        private_dm_id=request.admission.private_dm_id,
        origin_seal_ids=request.admission.origin_seal_ids,
    )


def source_instructions_preserved(required: str | None, system: str | None) -> bool:
    """Retained source text is an exact prefix, with a paragraph boundary."""
    return (
        required is None
        or system is not None
        and (system == required or system.startswith(required + "\n\n"))
    )


def compose_source_instructions(
    required: str | None, system: str | None, *, deduplicate_prefix: bool = False
) -> str:
    """Preserve the source prefix once, retaining the target's remaining paragraphs."""
    if source_instructions_preserved(required, system):
        return system or ""
    assert required is not None
    if not deduplicate_prefix:
        return required + ("\n\n" + system if system else "")
    suffix = (system or "").split("\n\n") if system else []
    common = 0
    for left, right in zip(required.split("\n\n"), suffix, strict=False):
        if left != right:
            break
        common += 1
    rest = "\n\n".join(suffix[common:])
    return required + ("\n\n" + rest if rest else "")


def classify_source_obligations(
    source: BackendAgent,
    target: AgentSpec | None,
    *,
    environment: EnvironmentSpec | None,
    resources: tuple[ResourceBinding, ...] = (),
    asks_before_publishing: bool = False,
) -> frozenset[str]:
    """One source-preservation rule for pre-allocation normalization and preparation."""
    spec = source.effective_spec
    if spec is None:
        return frozenset({"source_definition"})
    drops = target.declared_drops if target is not None else ()
    missing: set[str] = set()
    if not source_instructions_preserved(spec.system, target.system if target else None):
        missing.add("instructions")
    source_tools = tuple(spec.tools or ())
    if target is not None:
        from mux.drivers.anthropic.schemas import agent_tools

        for extension in spec.extensions or ():
            model = extension.value.get("model")
            if extension.version != 1:
                missing.add("source_definition")
            elif extension.namespace == "anthropic.agent_tools":
                try:
                    native = agent_tools(extension)
                    if "tools" not in drops:
                        source_tools = native.intent()
                except ValueError:
                    missing.update(("source_definition", "tools"))
            elif not (
                extension.namespace == "anthropic.model_config"
                and isinstance(model, dict)
                and set(model) <= {"id", "speed"}
                and model.get("speed") is None
                or extension.namespace == "anthropic.multiagent"
                and extension.value == {"clear": True}
                or extension.namespace == "anthropic.agent_create_nulls"
            ):
                missing.add("source_definition")
    profile = get_profile(backend_selection(spec.model.provider).profile)
    tools_required = bool(
        spec.tools or any(item.value.get("tools") for item in spec.extensions or ())
    )
    if asks_before_publishing or (
        tools_required
        and "tools" not in drops
        and (
            target is None
            or native_agent_tools(profile, spec.model_copy(update={"tools": source_tools}))
            != native_agent_tools(profile, target)
        )
    ):
        missing.add("tools")
    if spec.skills and "skills" not in drops and (target is None or target.skills != spec.skills):
        missing.add("skills")
    if (
        spec.mcp_servers
        and "mcp" not in drops
        and "tools" not in drops
        and (target is None or target.mcp_servers != spec.mcp_servers)
    ):
        missing.add("mcp")
    if source.repository_sources and "repository" not in drops:
        missing.add("repository")
    for resource in source.resource_requirements:
        if resource.target_path is not None and "files" not in drops:
            missing.add("files")
        # No installed materializer proves credentials or opaque private resources.
        missing.add("credentials" if resource.kind == "credential" else "source_definition")
    if (source.memory_required is not False or source.memory_store_id is not None) and (
        "memory" not in drops
    ):
        from daimon.core.memory_resource import memory_mount

        retained = False
        if len(resources) == 1 and source.memory_store_id is not None:
            native, ref = resources[0].native, resources[0].resource
            retained = (
                source.source is not None
                and source.resource_scope is not None
                and source.memory_read_only is not None
                and ref is not None
                and ref.provider == source.source.ref.provider
                and ref.kind == "memory_store"
                and ref.id == source.memory_store_id
                and (ref.tenant_id, ref.account_id)
                == (source.resource_scope.tenant_id, source.resource_scope.account_id)
                and native is not None
                and (native.namespace, native.version) == ("anthropic.session_resource_create", 1)
                and native.value
                in (
                    {"resource": memory_mount(source.memory_store_id, read_only=True)},
                    {
                        "resource": memory_mount(
                            source.memory_store_id, read_only=source.memory_read_only
                        )
                    },
                )
            )
        if not retained:
            missing.add("memory")
    # Removing execution is explicit; retaining a source environment requires
    # its complete canonical policy, which native readback must also verify.
    if target is None or not (
        ("sandbox" in drops and environment is None and not resources)
        or (
            source.environment is not None
            and environment is not None
            and environment == source.environment.spec
            and environment.native_config is None
        )
    ):
        missing.add("environment")
    if spec.model.options:
        missing.add("source_definition")
    return frozenset(missing)


async def prepare_source(request: ProviderPreparationRequest) -> ProviderPreparationRequest:
    """Prepare scoped source intent; native output verification remains mandatory."""
    from daimon.core.ma_identity import derive_agent_uuid

    revision = request.admission.backend_revision
    if (
        request.admission.backend_revision_origin != "agent"
        or not request.deps.agent_backends
        or not request.deps.channel_backends
        or request.deps.turn_path != "mux"
        or revision is None
        or request.scope.tenant_id != str(request.tenant_id)
        or request.scope.account_id != str(request.session_account_id)
        or request.scope.account_id != str(request.admission.account_id)
        or request.scope.is_platform
        or request.scope.is_legacy_host_authorized
    ):
        raise AdmissionDenied(reason="backend_unsupported")

    request = replace(
        request,
        deadline=turn_deadline(
            now=request.now(),
            deadline=request.deadline,
            authority_deadline=request.worker.limits.deadline
            if request.worker is not None
            else None,
        ),
    )

    timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
    source = await asyncio.wait_for(capture_preparation_source(request), timeout=timeout)
    if (
        source.tenant_id != request.tenant_id
        or source.agent_id
        != derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id)
        or source.resource_scope != request.scope
        or source.source is None
        or source.source.ref.kind != "agent"
        or source.source.ref.tenant_id != request.scope.tenant_id
        or source.source.lifecycle != "active"
        or source.source.ref.id != request.admission.agent.id
        or source.source.revision.local != request.admission.agent.version
    ):
        raise UnsupportedCapability(("source_context",), revision.profile)
    # Capture is evidence of source intent, not evidence that a target preserved it.
    spec = source.effective_spec
    if spec is None:
        raise UnsupportedCapability(("source_definition",), revision.profile)
    worker = request.worker
    if worker is not None:
        fleet = worker.permissions.fleet
        if (
            fleet.scope != request.scope
            or not fleet.enabled
            or not fleet.active
            or fleet.depth != 1
            or fleet.account_id != request.admission.account_id
            or fleet.root.channel != revision.channel
            or (worker.source_agent is None and worker.permissions.agent_id != source.agent_id)
            or worker.backend.profile != revision.profile
            or worker.backend.backend != revision.backend
            or worker.backend.model != revision.model
            or dict(worker.backend.requires) != dict(revision.requires)
            or worker.backend.thread_mode != revision.thread_mode
            or worker.permissions.spec != worker.spec
            or worker.permissions.resources != worker.resources
            or worker.permissions.environment != worker.environment
            or worker.permissions.limits != worker.limits
            or worker.permissions.subject.is_admin
            or (request.admission.memory_read_only and not worker.permissions.memory_read_only)
            or not request.admission.origin_seal_ids <= worker.permissions.seal_ids
            or (
                worker.source_agent is not None
                and worker.source_agent != (source.source.ref, source.source.revision)
            )
        ):
            raise UnsupportedCapability(("worker_context",), revision.profile)
    target = worker.spec if worker is not None else None
    missing = classify_source_obligations(
        source,
        target,
        environment=worker.environment if worker else None,
        resources=worker.resources if worker else (),
        asks_before_publishing=request.admission.asks_before_publishing,
    )
    if missing:
        raise UnsupportedCapability(tuple(sorted(missing)), revision.profile)
    return replace(request, source=source)


async def read_preparation_counterpart(
    request: ProviderPreparationRequest,
) -> ProviderPreparationRequest:
    """Verify the prepared source's owned native counterpart before handoff."""

    try:
        selected = await prepare_source(request)
        request = replace(request, deadline=selected.deadline)
        source, revision, worker = (
            selected.source,
            selected.admission.backend_revision,
            selected.worker,
        )
        if source is None or revision is None:
            raise AdmissionDenied(reason="backend_unsupported")
        target = worker.spec if worker is not None else None
        runtime = selected.deps.turn_runtimes.get(revision.profile)
        if runtime is None or runtime.read_agent_counterpart is None:
            raise UnsupportedCapability(("native_counterpart",), revision.profile)
        if runtime.materialize_worker is not None and selected.before_native is not None:
            from daimon.core.turn.persistence import current_persistence

            persistence = current_persistence.get()
            if (
                worker is None
                or persistence is None
                or persistence.store is not selected.deps.state_store
                or persistence.slot.thread.channel != revision.channel
                or persistence.slot.thread.thread_id != selected.thread_id
                or persistence.slot.account_id != selected.scope.account_id
            ):
                raise UnsupportedCapability(("worker_materialization",), revision.profile)
            persistence.check_session(selected.scope, None)
            timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
            await asyncio.wait_for(selected.before_native(), timeout=timeout)
            timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
            allocated = await asyncio.wait_for(
                runtime.materialize_worker(selected), timeout=timeout
            )
            selected = replace(selected, counterpart=allocated)
        timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        counterpart = await asyncio.wait_for(
            runtime.read_agent_counterpart(selected), timeout=timeout
        )
        if selected.counterpart is not None and counterpart != selected.counterpart:
            raise UnsupportedCapability(("worker_acknowledgement",), revision.profile)
        timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        current_source = await asyncio.wait_for(
            capture_preparation_source(request), timeout=timeout
        )
        remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        if target is None or counterpart.agent.spec != target or current_source != source:
            raise UnsupportedCapability(("source_drift",), revision.profile)
        return replace(selected, counterpart=counterpart, verified_counterpart=counterpart)
    except (UnsupportedCapability, TimeoutError, TurnError) as error:
        raise AdmissionDenied(reason="backend_unsupported") from error
    except Exception:
        raise AdmissionDenied(reason="backend_unsupported") from None


class CounterpartPlan(Protocol):
    """Read-only operator pins shared by native deployment plans."""

    @property
    def profile(self) -> str: ...
    @property
    def worker_materialization(self) -> bool: ...
    @property
    def tenant_id(self) -> str: ...
    @property
    def account_id(self) -> str: ...
    @property
    def platform(self) -> str: ...
    @property
    def channel_id(self) -> str: ...
    @property
    def project(self) -> str: ...
    @property
    def agent_id(self) -> str | None: ...
    @property
    def skill_ids(self) -> tuple[str, ...]: ...
    @property
    def source_agent_id(self) -> UUID | None: ...
    @property
    def source_agent_version(self) -> int | None: ...
    @property
    def counterpart_catalog_id(self) -> str | None: ...
    @property
    def counterpart_catalog_revision(self) -> int | None: ...
    @classmethod
    def model_validate(cls, obj: object) -> Self: ...


def validate_counterpart_pins(
    plan: CounterpartPlan, *, worker_materialization: bool = False
) -> None:
    """Legacy rows have no counterpart; partial source/catalog pins never authorize one."""
    if worker_materialization:
        if (
            plan.source_agent_id is None
            or plan.source_agent_version is None
            or plan.counterpart_catalog_id is not None
            or plan.counterpart_catalog_revision is not None
            or plan.agent_id is not None
        ):
            raise ValueError(
                "worker materialization requires source pins without fixed target pins"
            )
        return
    pins = (
        plan.source_agent_id,
        plan.source_agent_version,
        plan.counterpart_catalog_id,
        plan.counterpart_catalog_revision,
    )
    if any(pin is not None for pin in pins) and any(pin is None for pin in pins):
        raise ValueError("a counterpart requires complete source and catalog revision pins")
    if all(pin is not None for pin in pins) and plan.agent_id is None:
        raise ValueError("a counterpart requires a native agent identity")


def select_runtime_manifest[Plan: CounterpartPlan](
    request: ProviderPreparationRequest, plan_type: type[Plan]
) -> Plan:
    """Select exactly one installed scope/configuration grant before credential discovery."""

    revision = request.admission.backend_revision
    if revision is None:
        raise AdmissionDenied(reason="backend_unsupported")
    manifest_path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not manifest_path:
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        with Path(manifest_path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise AdmissionDenied(reason="backend_unsupported")
        selected = [
            row
            for row in _ROWS.validate_json(raw)
            if row.get("profile") == revision.profile
            and row.get("tenant_id") == request.scope.tenant_id
            and row.get("platform") == revision.channel.platform
            and row.get("channel_id") == revision.channel.channel_id
            and row.get("account_id") == request.scope.account_id
            and manifest_configuration_matches(row, revision)
        ]
        if len(selected) != 1:
            raise AdmissionDenied(reason="backend_unsupported")
        return plan_type.model_validate(selected[0])
    except (OSError, ValidationError):
        raise AdmissionDenied(reason="backend_unsupported") from None


def validate_counterpart_request(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    *,
    worker_materialization: bool = False,
) -> ConfigRevision:
    """Validate the same immutable source/configuration pins before native allocation."""
    from daimon.core.ma_identity import derive_agent_uuid

    try:
        validate_counterpart_pins(plan, worker_materialization=worker_materialization)
    except ValueError:
        raise AdmissionDenied(reason="backend_unsupported") from None
    revision, scope = request.admission.backend_revision, request.scope
    if (
        request.admission.backend_revision_origin != "agent"
        or revision is None
        or not request.deps.channel_backends
        or request.deps.turn_path != "mux"
        or scope.tenant_id != str(request.tenant_id)
        or scope.account_id != str(request.admission.account_id)
        or scope.account_id != str(request.session_account_id)
        or (plan.tenant_id, plan.account_id) != (scope.tenant_id, scope.account_id)
        or (revision.channel.tenant_id, revision.channel.platform, revision.channel.channel_id)
        != (plan.tenant_id, plan.platform, plan.channel_id)
        or request.platform != plan.platform
        or revision.profile != plan.profile
        or plan.source_agent_id
        != derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id)
        or plan.source_agent_version != request.admission.agent.version
        or (worker_materialization and (request.worker is None or request.before_native is None))
        or (
            not worker_materialization
            and (
                plan.counterpart_catalog_id is None
                or plan.counterpart_catalog_revision is None
                or plan.agent_id is None
            )
        )
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if select_runtime_manifest(request, type(plan)) != plan:
        raise AdmissionDenied(reason="backend_unsupported")
    return revision


async def materialize_counterpart(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    original: ProviderPreparationRequest,
    *,
    resources: Callable[[TurnPersistence, ResolvedWorkerSpec], Awaitable[NeutralResourceHost]],
) -> AgentRevision:
    """Publish through the held preparation owner after validating its original grant."""
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.persistence import current_persistence

    validate_counterpart_request(plan, request, worker_materialization=True)
    worker, source, guard = request.worker, request.source, request.before_native
    active = current_persistence.get()
    if (
        not plan.worker_materialization
        or (request.scope, request.admission.backend_revision, worker)
        != (original.scope, original.admission.backend_revision, original.worker)
        or worker is None
        or source is None
        or source.source is None
        or guard is None
        or active is None
        or active.scope != request.scope
        or active.store is not request.deps.state_store
        or request.deps.state_store is not original.deps.state_store
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    active.check_session(request.scope, None)
    host = await resources(active, worker)
    catalog_id = active.operation_key + ":agent"
    return await host.publish(
        catalog_id,
        worker.spec,
        expected_revision=0,
        key=catalog_id,
        persistence=active,
        guard=guard,
        worker=worker,
        source=source.source,
    )


async def read_counterpart(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    *,
    source: BackendAgent,
    resources: NeutralResourceHost,
    verify_native: Callable[[AgentRevision], Awaitable[None]],
    worker_materialization: bool = False,
) -> AgentRevision:
    """Verify an installed native catalog component, without authorizing execution.

    Complete source preservation is a separate owner's proof. Names and source
    digests cannot substitute for identity, version or operator configuration.
    Readback detects drift; it does not create a native CAS guarantee.
    """
    from daimon.core.skills.ingest import SkillIngestError
    from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
    from mux.profiles import get_profile

    revision = validate_counterpart_request(
        plan, request, worker_materialization=worker_materialization
    )
    scope = request.scope
    from daimon.core.turn.persistence import current_persistence

    persistence = current_persistence.get() if worker_materialization else None
    acknowledged = request.counterpart if worker_materialization else None
    if worker_materialization and (
        persistence is None
        or persistence.store is not request.deps.state_store
        or acknowledged is None
        or source.source is None
        or source.resource_scope != scope
        or request.worker is None
        or request.worker.permissions.fleet.scope != scope
        or persistence.slot.thread.channel != revision.channel
        or persistence.slot.thread.thread_id != request.thread_id
        or persistence.slot.account_id != scope.account_id
        or acknowledged.catalog_id != persistence.operation_key + ":agent"
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if persistence is not None:
        persistence.check_session(scope, None)
    catalog_id = acknowledged.catalog_id if acknowledged else cast(str, plan.counterpart_catalog_id)
    if (
        source.tenant_id != request.tenant_id
        or source.agent_id != plan.source_agent_id
        or resources.scope != scope
        or resources.workspace != plan.project
        or resources.backend.capabilities().profile_id != plan.profile
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        row = await resources.entry(catalog_id)
        if (
            (acknowledged is not None and row != acknowledged)
            or (
                not worker_materialization
                and (
                    row.local_revision != plan.counterpart_catalog_revision
                    or row.agent.ref.id != plan.agent_id
                )
            )
            or row.agent.spec.model.id != revision.model
            or (
                request.admission.backend_counterpart is not None
                and row != request.admission.backend_counterpart
            )
            or row.agent.spec.model.provider != get_profile(plan.profile).provider
            or tuple(pin.id for pin in row.agent.spec.skills or ()) != plan.skill_ids
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        checked = (
            await resources.read(
                catalog_id,
                store=persistence.store,
                key=persistence.operation_key + ":agent_create:0",
                worker=request.worker,
                source=source.source,
            )
            if persistence is not None
            else await resources.read(catalog_id)
        )
        await verify_native(checked)
        if (
            checked != row
            or select_runtime_manifest(request, type(plan)) != plan
            or await resources.entry(catalog_id) != row
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        return checked
    except (
        ValueError,
        KeyError,
        SkillIngestError,
        ProviderError,
        ScopeViolation,
        UnsupportedCapability,
    ):
        raise AdmissionDenied(reason="backend_unsupported") from None
