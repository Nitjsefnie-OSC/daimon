"""Credential-reference validation without credential discovery or SDK imports."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self
from uuid import UUID

from mux.contracts.config import CapabilityRequirement, ConfigRevision, ThreadMode
from mux.contracts.profile import Capability
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
    from daimon.core.neutral_resources import NeutralResourceHost
    from daimon.core.stores.domain import BackendAgent
    from daimon.core.stores.neutral_catalog import AgentRevision
    from daimon.core.turn.prepare import ProviderPreparationRequest

_RESERVED = ("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_")
_ROWS = TypeAdapter(list[dict[str, JsonValue]])
_MAX_BYTES = 1_048_576


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
    """An operator's fixed native plan is not an agent-counterpart proof."""
    return origin in (None, "channel")


class CounterpartPlan(Protocol):
    """Read-only operator pins shared by native deployment plans."""

    @property
    def profile(self) -> str: ...
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


def validate_counterpart_pins(plan: CounterpartPlan) -> None:
    """Legacy rows have no counterpart; partial source/catalog pins never authorize one."""
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
    from daimon.core.turn.errors import AdmissionDenied

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


async def read_counterpart(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    *,
    source: BackendAgent,
    resources: NeutralResourceHost,
    verify_native: Callable[[AgentRevision], Awaitable[None]],
) -> AgentRevision:
    """Verify an installed native catalog component, without authorizing execution.

    Complete source preservation is a separate owner's proof. Names and source
    digests cannot substitute for identity, version or operator configuration.
    Readback detects drift; it does not create a native CAS guarantee.
    """
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.skills.ingest import SkillIngestError
    from daimon.core.turn.errors import AdmissionDenied
    from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
    from mux.profiles import get_profile

    revision, scope = request.admission.backend_revision, request.scope
    if (
        getattr(request.admission, "backend_revision_origin", None) != "agent"
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
        or source.tenant_id != request.tenant_id
        or source.agent_id != plan.source_agent_id
        or source.agent_id
        != derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id)
        or plan.source_agent_version != request.admission.agent.version
        or plan.counterpart_catalog_id is None
        or plan.counterpart_catalog_revision is None
        or plan.agent_id is None
        or resources.scope != scope
        or resources.workspace != plan.project
        or resources.backend.capabilities().profile_id != plan.profile
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if select_runtime_manifest(request, type(plan)) != plan:
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        row = await resources.entry(plan.counterpart_catalog_id)
        if (
            row.local_revision != plan.counterpart_catalog_revision
            or row.agent.ref.id != plan.agent_id
            or row.agent.spec.model.id != revision.model
            or row.agent.spec.model.provider != get_profile(plan.profile).provider
            or tuple(pin.id for pin in row.agent.spec.skills or ()) != plan.skill_ids
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        checked = await resources.read(plan.counterpart_catalog_id)
        await verify_native(checked)
        if (
            checked != row
            or select_runtime_manifest(request, type(plan)) != plan
            or await resources.entry(plan.counterpart_catalog_id) != row
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
