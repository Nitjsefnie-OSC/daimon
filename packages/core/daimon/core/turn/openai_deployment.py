"""Operator-owned OpenAI deployment plans, loaded only for explicit admission.

The manifest contains scoped provisioned identities and credential environment
names, never credentials. Transport construction follows the preparer's policy
recheck. No SDK client or provider configuration is loaded during app startup.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_host import OpenAIHostRuntime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.turn.runtime_manifest import (
    ApiKeyEnvironment,
    ConfigurationGrant,
    fixed_plan_origin_allowed,
    read_counterpart,
    select_runtime_manifest,
    validate_counterpart_pins,
)
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import SessionSpec
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import Transport
from mux.errors import ScopeViolation
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

Nonblank = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
_MANIFEST = TypeAdapter(list[dict[str, JsonValue]])
_MAX_MANIFEST_BYTES = 1_048_576


class OpenAIDeployment(BaseModel):
    """An operator authorization for one account's exact channel configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["openai.persistent_workspace"]
    tenant_id: Nonblank
    platform: Nonblank
    channel_id: Nonblank
    account_id: Nonblank
    config_digest: Nonblank | None = None
    configuration_grant: ConfigurationGrant | None = None
    project: Nonblank
    agent_id: Nonblank
    environment_id: Nonblank
    api_key_env: ApiKeyEnvironment
    spend_limit_usd_cents: Annotated[int, Field(gt=0, strict=True)] | None = None
    skill_ids: tuple[Nonblank, ...] = ()
    source_agent_id: UUID | None = None
    source_agent_version: Annotated[int, Field(gt=0, strict=True)] | None = None
    counterpart_catalog_id: Nonblank | None = None
    counterpart_catalog_revision: Annotated[int, Field(gt=0, strict=True)] | None = None

    @model_validator(mode="after")
    def _configuration_authorization(self) -> OpenAIDeployment:
        if self.config_digest is None and self.configuration_grant is None:
            raise ValueError("a deployment requires exact or bounded configuration authorization")
        validate_counterpart_pins(self)
        return self

    async def read_counterpart(
        self,
        request: ProviderPreparationRequest,
        *,
        source: BackendAgent,
        resources: NeutralResourceHost,
    ) -> AgentRevision:
        """Read the operator-pinned catalog component; complete source proof remains separate."""

        async def verify_native(checked: AgentRevision) -> None:
            scope = request.scope
            environment = await resources.backend.environments.retrieve(
                scope,
                ResourceRef(
                    id=self.environment_id,
                    kind="environment",
                    provider="openai",
                    account_scope_id=self.project,
                    tenant_id=scope.tenant_id,
                    account_id=scope.account_id,
                ),
            )
            if environment.lifecycle != "active" or environment.spec.execution != "hosted":
                raise AdmissionDenied(reason="backend_unsupported")

        return await read_counterpart(
            self, request, source=source, resources=resources, verify_native=verify_native
        )


def load_deployment(request: ProviderPreparationRequest) -> OpenAIDeployment:
    if not fixed_plan_origin_allowed(getattr(request.admission, "backend_revision_origin", None)):
        raise AdmissionDenied(reason="backend_unsupported")
    return _manifest_deployment(request)


def _manifest_deployment(request: ProviderPreparationRequest) -> OpenAIDeployment:
    revision = request.admission.backend_revision
    if revision is None or revision.profile != "openai.persistent_workspace":
        raise AdmissionDenied(reason="backend_unsupported")
    return select_runtime_manifest(request, OpenAIDeployment)


def authorized_deployments(tenant_id: str) -> tuple[OpenAIDeployment, ...]:
    """Every explicit OpenAI deployment the operator's manifest grants one tenant.

    For callers outside a turn (the CLI, MCP tools) that route a tenant's or
    channel's resources natively only where the operator authorized it. The
    same manifest and limits as `load_deployment`. No manifest, no row for the
    tenant, or a file that cannot be read or validated means none: nothing is
    authorized, and callers keep their existing path.
    """
    manifest_path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not manifest_path:
        return ()
    try:
        with Path(manifest_path).open("rb") as manifest:
            raw = manifest.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_MANIFEST_BYTES:
            return ()
        return tuple(
            OpenAIDeployment.model_validate(row)
            for row in _MANIFEST.validate_json(raw)
            if row.get("profile") == "openai.persistent_workspace"
            and row.get("tenant_id") == tenant_id
        )
    except (OSError, ValidationError):
        return ()


def build_openai_runtime(request: ProviderPreparationRequest) -> OpenAIHostRuntime:
    revision = request.admission.backend_revision
    if revision is None or revision.model != "gpt-6-luna":
        raise AdmissionDenied(reason="backend_unsupported")
    deployment = load_deployment(request)
    scope = request.scope

    def check(config: ConfigRevision, authorized: Scope) -> None:
        if config != revision or authorized != scope:
            raise ScopeViolation(request.thread_id, "deployment differs from admitted turn")

    async def plan(preparation: ProviderPreparationRequest) -> SessionSpec:
        if preparation.admission.backend_revision != revision or preparation.scope != scope:
            raise ScopeViolation(request.thread_id, "native plan differs from admitted turn")

        def ref(id_: str, kind: str) -> ResourceRef:
            return ResourceRef(
                id=id_,
                kind=kind,
                provider="openai",
                account_scope_id=deployment.project,
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            )

        return SessionSpec(
            agent=ref(deployment.agent_id, "agent"),
            agent_revision=Revision(local=0),
            environment=ref(deployment.environment_id, "environment"),
            config_revision=revision.local,
        )

    def authorize(authorized: Scope, kind: str, id_: str | None) -> bool:
        if authorized != scope:
            return False
        if kind == "session":
            # The host driver obtains existing sessions from its persisted scoped
            # binding; creation publishes the acknowledged native identity there.
            return True
        return id_ is not None and (kind, id_) in {
            ("agent", deployment.agent_id),
            ("environment", deployment.environment_id),
            *(("skill", skill) for skill in deployment.skill_ids),
        }

    def transport(config: ConfigRevision, authorized: Scope) -> Transport:
        check(config, authorized)
        secret = os.environ.get(deployment.api_key_env)
        if secret is None or not secret.strip():
            raise AdmissionDenied(reason="backend_unsupported")
        # Import/construction is selected-only. The private driver owns SDK
        # request/stream lifetimes and disables retry of uncertain mutations.
        from mux.drivers.openai.deployment import configured_transport

        return configured_transport(api_key=secret, project=deployment.project)

    return OpenAIHostRuntime(
        transport_factory=transport,
        journal=OpenAIRecoveryJournal(),
        usage_store=OpenAIUsageRevisions(),
        account_scope_id=deployment.project,
        session_plan=plan,
        authorization=authorize,
        controls=SessionControls(
            model="gpt-6-luna",
            multi_agent_enabled=False,
            container_size="small",
            spend_limit_usd_cents=deployment.spend_limit_usd_cents,
        ),
    )
